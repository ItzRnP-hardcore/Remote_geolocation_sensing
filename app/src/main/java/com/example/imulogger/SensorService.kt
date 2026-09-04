package com.example.imulogger

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.GnssMeasurementsEvent
import android.location.GnssNavigationMessage
import android.location.GnssStatus
import android.location.Location
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.Process
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service that records a time-synchronised IMU + GNSS trace to disk.
 *
 * Threading contract: every sensor callback, location callback, GNSS status callback and file
 * write happens on [loggerThread]. Nothing touches the main thread except [status] publication,
 * which goes through a StateFlow. Because there is exactly one writer thread, no locking is
 * needed around the buffers or the counters.
 *
 * Timebase contract: all rows are stamped in nanoseconds on the SystemClock.elapsedRealtimeNanos
 * timebase, the same base used by SensorEvent.timestamp and Location.getElapsedRealtimeNanos.
 * That clock is monotonic and does not jump when NTP corrects the wall clock, which is what a
 * dead-reckoning filter needs in order to compute dt. session.json records the mapping from this
 * timebase to UTC so a trace can still be aligned against external references.
 */
class SensorService : Service() {

    companion object {
        private const val TAG = "SensorService"

        const val ACTION_STOP = "com.example.imulogger.action.STOP"

        /**
         * Withhold GNSS from the dead-reckoning integrator so it free-runs on the IMU. Lets the
         * tunnel case be exercised on an open road instead of waiting for a real outage.
         */
        fun setFreeRun(enabled: Boolean) { freeRunRequested = enabled }

        @Volatile
        private var freeRunRequested = false

        private const val CHANNEL_ID = "recording"
        private const val NOTIFICATION_ID = 1

        // Sampling periods in microseconds.
        private const val HZ_200 = 5_000
        private const val HZ_100 = 10_000
        private const val HZ_50 = 20_000
        private const val HZ_25 = 40_000

        /**
         * Let the sensor hub batch up to a second of samples in its hardware FIFO before waking
         * the application processor. Batched events keep their individual, correct timestamps.
         */
        private const val MAX_REPORT_LATENCY_US = 1_000_000

        private const val FLUSH_INTERVAL_MS = 2_000L

        /** mapmatch.csv `mode` column: which estimator produced the row. */
        private const val MODE_MATCH = "match"
        private const val MODE_ALONG_ROAD = "alongroad"

        /** The model was trained on 10 Hz windows, so it must be fed at 10 Hz. */
        private const val ML_PERIOD_NS = 100_000_000L

        /**
         * Cap on inference requests queued behind a slow forward pass. Dropping a request costs
         * one window update; letting the queue grow costs unbounded memory and ever-staler output.
         */
        private const val ML_MAX_PENDING = 8
        private const val WAKELOCK_TIMEOUT_MS = 12L * 60 * 60 * 1000

        private val _status = MutableStateFlow(LoggerStatus())

        /** Observed by the UI. Safe to collect from anywhere. */
        val status: StateFlow<LoggerStatus> = _status.asStateFlow()

        private val _track = MutableStateFlow<List<TrackPoint>>(emptyList())

        /**
         * Fixes from the current session, for drawing on the map. Kept separate from [status] so a
         * two-second counter refresh does not force the map to rebuild a polyline that has not
         * changed, and so the track survives on screen after recording stops.
         */
        val track: StateFlow<List<TrackPoint>> = _track.asStateFlow()

        private val _drTrack = MutableStateFlow<List<TrackPoint>>(emptyList())

        /** The IMU-only track, for drawing beside [track] so the divergence is visible. */
        val drTrack: StateFlow<List<TrackPoint>> = _drTrack.asStateFlow()

        private val _snapTrack = MutableStateFlow<List<TrackPoint>>(emptyList())

        /** The dead-reckoned track after being snapped onto the road network. */
        val snapTrack: StateFlow<List<TrackPoint>> = _snapTrack.asStateFlow()
    }

    /** One recorded sensor stream: what to register, how fast, and what to call it in the CSV. */
    private data class StreamSpec(val type: Int, val label: String, val periodUs: Int)

    private val streams = listOf(
        // Raw inertial data, the primary dead-reckoning input.
        StreamSpec(Sensor.TYPE_ACCELEROMETER, "accel", HZ_200),
        StreamSpec(Sensor.TYPE_GYROSCOPE, "gyro", HZ_200),
        // Uncalibrated variants also report the bias the OS is subtracting. A filter that
        // estimates its own bias states wants the raw signal, not one already "helped" by the OS.
        StreamSpec(Sensor.TYPE_ACCELEROMETER_UNCALIBRATED, "accel_uncal", HZ_200),
        StreamSpec(Sensor.TYPE_GYROSCOPE_UNCALIBRATED, "gyro_uncal", HZ_200),
        // Heading reference.
        StreamSpec(Sensor.TYPE_MAGNETIC_FIELD, "mag", HZ_50),
        StreamSpec(Sensor.TYPE_MAGNETIC_FIELD_UNCALIBRATED, "mag_uncal", HZ_50),
        // Attitude. GAME_ROTATION_VECTOR excludes the magnetometer so it does not swing when the
        // car body distorts the field; ROTATION_VECTOR includes it for absolute heading.
        StreamSpec(Sensor.TYPE_GAME_ROTATION_VECTOR, "game_rv", HZ_100),
        StreamSpec(Sensor.TYPE_ROTATION_VECTOR, "rv", HZ_50),
        // The vendor gravity/linear split is a useful cross-check on our own attitude estimate.
        StreamSpec(Sensor.TYPE_GRAVITY, "gravity", HZ_50),
        StreamSpec(Sensor.TYPE_LINEAR_ACCELERATION, "linear_accel", HZ_100),
        // Barometer: altitude change, and the pressure step at a tunnel mouth.
        StreamSpec(Sensor.TYPE_PRESSURE, "pressure", HZ_25),
    )

    private val sensorTypeLabels: Map<Int, String> = streams.associate { it.type to it.label }

    private lateinit var sensorManager: SensorManager
    private lateinit var locationManager: LocationManager
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var loggerThread: HandlerThread
    private lateinit var loggerHandler: Handler

    /**
     * Inference lives on its own thread. A forward pass through the ResNet takes tens of
     * milliseconds; on the logger thread that would stall 200 Hz sensor delivery and the CSV
     * writes, and on the main thread it would be an ANR.
     */
    private lateinit var mlThread: HandlerThread
    private lateinit var mlHandler: Handler
    private val mlPending = java.util.concurrent.atomic.AtomicInteger(0)
    private var mlDropped: Long = 0

    private var wakeLock: PowerManager.WakeLock? = null
    private val registeredSensors = mutableListOf<Sensor>()

    /**
     * Set synchronously on the main thread in onStartCommand. The published status cannot serve
     * as the guard because the session is set up asynchronously on the logger thread, so two
     * start commands in quick succession would both get past it and register every listener twice.
     */
    private var sessionActive = false

    private var imuWriter: BufferedWriter? = null
    private var gpsWriter: BufferedWriter? = null
    private var gnssWriter: BufferedWriter? = null

    /**
     * Raw per-satellite observables, one row per satellite per epoch.
     *
     * [gnssWriter] records how many satellites the receiver used; this records what each one
     * actually measured, which is a different kind of data entirely. The reason it exists is
     * that a position fix needs four satellites for four unknowns, but a *velocity* fix from
     * Doppler needs only as many as there are unknowns left after the vehicle's own constraints
     * are applied - a flat road removes vertical velocity, the non-holonomic constraint reduces
     * velocity to a scalar along the gyro's heading, and coasting the clock drift removes the
     * last one. In simulation that reaches 0.40 m/s of speed error from a SINGLE satellite,
     * against the integrator's 1.39. See eval/scarce_gnss.py.
     *
     * That result is simulated, and it stays simulated until there is real Doppler on disk.
     * This is the file that makes validating it possible.
     */
    private var gnssRawWriter: BufferedWriter? = null

    /** Broadcast navigation messages, so satellite positions can be reconstructed offline. */
    private var gnssNavWriter: BufferedWriter? = null

    /** Whether the chipset accepted each raw-GNSS registration. Recorded in session.json. */
    private var gnssRawSupported = false
    private var gnssNavSupported = false
    private var gnssRawRows = 0L
    private var gnssNavRows = 0L

    private var sessionDir: File? = null
    private var sessionId: String = ""
    private var sessionStartRealtimeNs: Long = 0
    
    // ML Model Integration
    private lateinit var imuModelRunner: IMUModelRunner
    private val lastAccel = FloatArray(3)
    private val lastGyro = FloatArray(3)
    private val lastGrav = FloatArray(3)
    /** Set on the ML thread once the module is loaded, read on the logger thread every gyro tick. */
    @Volatile private var modelReady = false
    
    // Calibration Logic
    private var calibrationStartTimeNs = 0L
    private var isCalibrating = true

    // Everything below is touched only on the logger thread.
    private var imuSamples: Long = 0
    private var gpsFixes: Long = 0
    private var writeErrors: Long = 0
    private var lastFixRealtimeNs: Long = 0
    private var lastLat: Double? = null
    private var lastLon: Double? = null
    private var lastSpeed: Float? = null
    private var lastBearing: Float? = null
    private var lastAccuracy: Float? = null
    private var distanceMetres: Double = 0.0
    private var lastDistanceLat: Double = Double.NaN
    private var lastDistanceLon: Double = Double.NaN
    private var satellitesVisible: Int = 0
    private var satellitesUsedInFix: Int = 0
    private var meanCn0: Float = 0f
    
    // ML tracking
    private var lastMLFeedTimeNanos: Long = 0L
    private var mlWriter: BufferedWriter? = null

    /**
     * Reused across events. getRotationMatrix runs on every gyro sample, and allocating two
     * FloatArray(9) plus a FloatArray(3) at 200 Hz is 600 short-lived objects a second.
     */
    private val rotationMatrix = FloatArray(9)

    /** Scratch for the debiased gyro handed to the model. Sensor thread only, like [lastGyro]. */
    private val gyroForModel = FloatArray(3)
    private val orientationAngles = FloatArray(3)

    /** Written on the logger thread, read by the periodic publish. */
    private var latestAzimuthDeg: Float = 0f

    /** Latest model outputs, written on the ML thread and published on the periodic tick. */
    @Volatile private var mlMu: Float = Float.NaN
    @Volatile private var mlLogvar: Float = Float.NaN
    @Volatile private var mlStationaryLogit: Float = Float.NaN
    @Volatile private var mlYawRate: Float = Float.NaN
    @Volatile private var mlInferences: Long = 0

    /** Reused so the hot path does not allocate a builder per sample. */
    private val rowBuilder = StringBuilder(160)

    /** Logger-thread-owned; a snapshot is published to [track] on the periodic tick. */
    private val trackPoints = ArrayList<TrackPoint>()
    private var trackDirty = false

    private val deadReckoner = DeadReckoner()

    /**
     * Map matching gets its own thread: reading a Mapsforge tile is disk I/O and the first read of
     * a new area parses a few hundred ways, neither of which belongs on the 200 Hz logger thread.
     */
    private lateinit var matchThread: HandlerThread
    private lateinit var matchHandler: Handler
    private var roadNetwork: RoadNetwork? = null
    private var mapMatcher: MapMatcher? = null

    /** Which file the matcher is running against, surfaced in the status so the UI can say so. */
    @Volatile private var matchMapName: String? = null

    /**
     * Along-road tracking, used instead of the matcher once the integrator has drifted far enough
     * to be worth overriding rather than nudging. Owned by the matcher thread like the other two.
     */
    private val alongRoad = AlongRoadTracker()

    /** When the integrator last lost its GNSS anchor, or 0 while it is anchored. */
    private var unaidedSinceNs = 0L

    /** Distance the integrator reports since the previous tick; what drives the along-road walk. */
    private var lastDrLat = Double.NaN
    private var lastDrLon = Double.NaN
    @Volatile private var alongRoadActive = false
    private var matchWriter: BufferedWriter? = null
    private val snapPoints = ArrayList<TrackPoint>()
    @Volatile private var snapLat: Double? = null
    @Volatile private var snapLon: Double? = null
    @Volatile private var snapCorrection: Double = 0.0
    @Volatile private var snapRoadClass: String? = null
    @Volatile private var snapConfidence: Double = 0.0
    @Volatile private var headingCorrectionDeg: Double = 0.0
    private val drPoints = ArrayList<TrackPoint>()
    private var drWriter: BufferedWriter? = null
    private var lastDrSampleNs = 0L

    // ------------------------------------------------------------------ lifecycle

    override fun onCreate() {
        super.onCreate()
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        locationManager = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)

        loggerThread = HandlerThread("imu-logger", Process.THREAD_PRIORITY_FOREGROUND)
        loggerThread.start()
        loggerHandler = Handler(loggerThread.looper)

        matchThread = HandlerThread("map-match", Process.THREAD_PRIORITY_BACKGROUND)
        matchThread.start()
        matchHandler = Handler(matchThread.looper)
        matchHandler.post {
            val maps = MapsforgeSource.mapFiles(this)
            if (maps.isEmpty()) {
                Log.w(TAG, "No offline map installed; map matching disabled")
            } else {
                val net = RoadNetwork(maps.first())
                if (net.open()) {
                    roadNetwork = net
                    mapMatcher = MapMatcher(net)
                    matchMapName = maps.first().name
                    Log.i(TAG, "Map matching ready against ${maps.first().name}")
                }
            }
        }

        mlThread = HandlerThread("imu-ml", Process.THREAD_PRIORITY_DEFAULT)
        mlThread.start()
        mlHandler = Handler(mlThread.looper)

        // Loading the model materialises a 15 MB asset and builds a TorchScript module. Doing
        // that inline in onCreate blocks the main thread for long enough to risk an ANR, so it
        // happens on the ML thread and the service simply records nothing until it is ready.
        mlHandler.post {
            try {
                imuModelRunner = IMUModelRunner(this)
                modelReady = true
                Log.i(TAG, "ML model ready")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to load ML model; continuing without it", e)
            }
        }
    }

    /**
     * Hand one levelled sample to the model thread.
     *
     * Called on the logger thread at 10 Hz. Requests are dropped rather than queued without bound
     * when inference falls behind, because a backlog only produces increasingly stale predictions.
     */
    private fun submitToModel(
        tNs: Long,
        eax: Float, eay: Float, eaz: Float,
        gx: Float, gy: Float, gz: Float,
    ) {
        if (mlPending.get() >= ML_MAX_PENDING) {
            mlDropped++
            return
        }
        mlPending.incrementAndGet()
        mlHandler.post {
            try {
                val out = imuModelRunner.processIMUData(eax, eay, eaz, gx, gy, gz) ?: return@post
                val mu = out[IMUModelRunner.IDX_MU]
                val logvar = out[IMUModelRunner.IDX_LOGVAR]
                val stationary = out[IMUModelRunner.IDX_STATIONARY]
                val yaw = out[IMUModelRunner.IDX_YAW_RATE]
                mlMu = mu
                mlLogvar = logvar
                mlStationaryLogit = stationary
                mlYawRate = yaw
                mlInferences++
                // Writing goes back to the logger thread so there stays exactly one writer and
                // the no-locking invariant in this class holds.
                loggerHandler.post {
                    writeMlRow(tNs, mu, logvar, stationary, yaw)
                    fuseModelSpeed(mu, logvar, stationary)
                }
            } finally {
                mlPending.decrementAndGet()
            }
        }
    }

    /** Runs on the logger thread. */
    private fun writeMlRow(tNs: Long, mu: Float, logvar: Float, stationary: Float, yaw: Float) {
        val sb = rowBuilder
        sb.setLength(0)
        sb.append(tNs).append(',')
            .append(mu).append(',')
            .append(logvar).append(',')
            .append(stationary).append(',')
            .append(yaw).append('\n')
        writeRow(mlWriter, sb)
    }

    /**
     * Runs on the logger thread: let the model's speed estimate correct the integrator.
     *
     * Applied only while unaided. GNSS velocity is better than anything a network inferring from
     * a phone IMU can offer, so whenever a fix is arriving the model has nothing to add and every
     * opportunity to do harm.
     *
     * Off by default - see [IMUModelRunner.speedFusionEnabled] for the measurement that keeps it
     * that way. The rest of this is ready for a checkpoint worth trusting.
     */
    private fun fuseModelSpeed(mu: Float, logvar: Float, stationaryLogit: Float) {
        if (!IMUModelRunner.speedFusionEnabled) return
        if (!unaidedNow()) return
        // A confident stand-still call is an observation that speed is zero, not a reason to skip:
        // it is the one prediction the model can make that the integrator cannot check for itself.
        val target =
            if (stationaryLogit >= IMUModelRunner.STATIONARY_LOGIT_THRESHOLD) 0.0
            else mu.toDouble()
        deadReckoner.applyModelSpeed(target, IMUModelRunner.fusionWeight(logvar))
    }

    /** True when the integrator is running without a GNSS anchor. Logger thread. */
    private fun unaidedNow(): Boolean =
        freeRunRequested ||
            lastFixRealtimeNs == 0L ||
            (SystemClock.elapsedRealtimeNanos() - lastFixRealtimeNs) > 5_000_000_000L

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }

        // A START_STICKY restart after a process kill arrives with a null intent, and a second
        // tap on Start would otherwise register every listener a second time.
        if (sessionActive) return START_STICKY

        if (!hasLocationPermission()) {
            fail("Location permission is not granted.")
            return START_NOT_STICKY
        }

        return try {
            sessionActive = true
            startForegroundWithType()
            // Everything after this point — opening files, registering listeners, writing rows —
            // happens on the logger thread, so there is exactly one thread touching the writers
            // and the counters and no synchronisation is needed anywhere.
            loggerHandler.post { beginSession() }
            START_STICKY
        } catch (e: Exception) {
            Log.e(TAG, "Could not start recording", e)
            fail(e.message ?: e.javaClass.simpleName)
            START_NOT_STICKY
        }
    }

    override fun onDestroy() {
        sessionActive = false
        // Unregister first so nothing new is queued, then let the logger thread drain what is
        // already queued before the files are closed.
        sensorManager.unregisterListener(sensorListener)
        teardownLocation()
        locationSubscribed = false
        if (providerWatcherRegistered) {
            try {
                unregisterReceiver(providerWatcher)
            } catch (e: Exception) {
                Log.w(TAG, "Provider watcher was not registered", e)
            }
            providerWatcherRegistered = false
        }

        matchHandler.removeCallbacksAndMessages(null)
        matchHandler.post { alongRoad.reset(); roadNetwork?.close() }
        matchThread.quitSafely()
        try {
            matchThread.join(2_000)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }

        mlHandler.removeCallbacksAndMessages(null)
        mlThread.quitSafely()
        try {
            mlThread.join(2_000)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }

        loggerHandler.removeCallbacks(periodicTask)
        loggerHandler.post { closeSession() }
        loggerThread.quitSafely()
        try {
            loggerThread.join(3_000)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }

        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null

        _status.value = status.value.copy(running = false)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ------------------------------------------------------------------ session

    /** Runs on the logger thread. */
    private fun beginSession() {
        sessionId = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        // getExternalFilesDir can return null when external storage is unavailable; falling back
        // to internal storage keeps the session alive instead of throwing inside the FileWriter.
        val root = getExternalFilesDir(null) ?: filesDir
        val dir = File(root, "sessions/$sessionId")
        if (!dir.mkdirs() && !dir.isDirectory) {
            fail("Could not create " + dir.absolutePath)
            return
        }

        sessionDir = dir
        sessionStartRealtimeNs = SystemClock.elapsedRealtimeNanos()
        lastFixRealtimeNs = 0
        imuSamples = 0
        gpsFixes = 0
        writeErrors = 0
        distanceMetres = 0.0
        lastDistanceLat = Double.NaN
        lastDistanceLon = Double.NaN
        trackPoints.clear()
        drPoints.clear()
        snapPoints.clear()
        _snapTrack.value = emptyList()
        snapLat = null; snapLon = null; snapCorrection = 0.0
        snapRoadClass = null; snapConfidence = 0.0; headingCorrectionDeg = 0.0
        unaidedSinceNs = 0L
        lastDrLat = Double.NaN
        lastDrLon = Double.NaN
        alongRoadActive = false
        matchHandler.post { mapMatcher?.reset(); alongRoad.reset() }
        deadReckoner.reset()
        calibrationStartTimeNs = 0L
        isCalibrating = true
        lastMLFeedTimeNanos = 0L
        mlDropped = 0
        mlInferences = 0
        if (modelReady) mlHandler.post { imuModelRunner.reset() }
        _track.value = emptyList()
        _drTrack.value = emptyList()

        imuWriter = openWriter(dir, "imu.csv")
        imuWriter?.write("t_ns,sensor,accuracy,v0,v1,v2,v3,v4,v5\n")

        gpsWriter = openWriter(dir, "gps.csv")
        gpsWriter?.write(
            "t_ns,utc_ms,provider,lat,lon,alt_m,speed_mps,bearing_deg," +
                "acc_m,vert_acc_m,speed_acc_mps,bearing_acc_deg\n"
        )

        gnssWriter = openWriter(dir, "gnss_status.csv")
        gnssWriter?.write("t_ns,sats_visible,sats_used,mean_cn0_used_dbhz,max_cn0_dbhz\n")

        // The clock fields repeat on every row rather than living in a sidecar. They are
        // per-epoch, not per-satellite, so this is redundant - but a pseudorange rate is
        // meaningless without the clock drift it was measured against, and one self-contained
        // row per observable is far harder to mis-join later than two files and a timestamp.
        gnssRawWriter = openWriter(dir, "gnss_raw.csv")
        gnssRawWriter?.write(
            "t_ns,clock_time_ns,full_bias_ns,bias_ns,drift_nsps,drift_unc_nsps," +
                "hw_clock_discontinuity,leap_second," +
                "svid,constellation,state,received_sv_time_ns,received_sv_time_unc_ns," +
                "cn0_dbhz,pr_rate_mps,pr_rate_unc_mps," +
                "adr_m,adr_unc_m,adr_state,carrier_freq_hz,multipath\n")

        gnssNavWriter = openWriter(dir, "gnss_nav.csv")
        gnssNavWriter?.write("t_ns,type,svid,message_id,submessage_id,status,data_base64\n")

        matchWriter = openWriter(dir, "mapmatch.csv")
        matchWriter?.write(
            "t_ns,dr_lat,dr_lon,snap_lat,snap_lon,correction_m,road_class,confidence," +
                "heading_applied_deg,mode\n"
        )

        mlWriter = openWriter(dir, "ml.csv")
        mlWriter?.write("t_ns,mu,logvar,stationary_logit,yaw_rate\n")

        drWriter = openWriter(dir, "deadreckon.csv")
        drWriter?.write("t_ns,lat,lon,speed_mps,drift_m,bias_e,bias_n,bias_u,stationary,free_run,gyro_bias_x,gyro_bias_y,gyro_bias_z,gyro_bias_valid\n")

        acquireWakeLock()
        registerSensors()
        registerProviderWatcher()
        ensureLocationSubscribed()
        writeSessionMetadata(dir)

        loggerHandler.post(periodicTask)

        _status.value = LoggerStatus(
            running = true,
            sessionId = sessionId,
            sessionPath = dir.absolutePath,
            matchMap = matchMapName,
        )
    }

    /** Runs on the matcher thread: HMM snap, used while drift is still small enough to nudge. */
    private fun runMapMatcher(
        tNs: Long,
        dr: TrackPoint,
        courseDeg: Double?,
        speedMps: Double,
        uncertaintyM: Double,
    ) {
        val m = mapMatcher?.update(dr.lat, dr.lon, courseDeg, speedMps, uncertaintyM) ?: return
        snapLat = m.lat; snapLon = m.lon
        snapCorrection = m.correctionM
        snapRoadClass = m.roadClass
        snapConfidence = m.confidence
        loggerHandler.post {
            recordSnap(
                tNs, dr, m.lat, m.lon, m.correctionM, m.roadClass, m.confidence,
                m.roadBearingDeg, m.confidence >= MapMatcher.HEADING_FEEDBACK_MIN_CONFIDENCE,
                MODE_MATCH,
            )
        }
    }

    /**
     * Runs on the matcher thread: keep a walk along the road network in step with the integrator.
     *
     * Started on the first unaided tick, when drift is still nil and the position is therefore
     * still the GNSS anchor. That timing is the whole trick: entering the road from a known-good
     * position costs nothing, whereas entering it later from a drifted one bakes the drift in as a
     * permanent along-track offset. Measured, late entry was 20% worse than the baseline while
     * anchor entry was 42% better - the same code, differing only in where it started.
     *
     * The walk is maintained from then on but only becomes the reported estimate once [report] is
     * set, so the early seconds - where plain dead reckoning wins - stay on dead reckoning.
     */
    private fun maintainAlongRoad(
        tNs: Long,
        dr: TrackPoint,
        courseDeg: Double?,
        stepM: Double,
        report: Boolean,
    ) {
        val graph = roadNetwork?.graphNear(dr.lat, dr.lon) ?: return
        // Crossing into a new tile block yields a new graph, whose segment indices mean nothing to
        // the old walk. Re-localise from the walk's own last position rather than the integrator's,
        // so a re-entry mid-outage does not import the drift the walk exists to avoid.
        if (!alongRoad.isTrackingOn(graph)) {
            val fromLat = if (alongRoadActive) snapLat ?: dr.lat else dr.lat
            val fromLon = if (alongRoadActive) snapLon ?: dr.lon else dr.lon
            if (!alongRoad.start(graph, fromLat, fromLon, courseDeg)) return
        }
        val fix = alongRoad.advance(stepM) ?: return
        if (!report) return

        alongRoadActive = true
        snapLat = fix.lat; snapLon = fix.lon
        snapCorrection = metresBetween(dr.lat, dr.lon, fix.lat, fix.lon)
        snapRoadClass = fix.roadClass
        // One live route means the network itself has resolved the ambiguity; several means a
        // junction is still unresolved, and the heading should not be trusted yet.
        snapConfidence = if (fix.alternatives <= 1) 1.0 else 1.0 / fix.alternatives
        val confidence = snapConfidence
        val correction = snapCorrection
        loggerHandler.post {
            recordSnap(
                tNs, dr, fix.lat, fix.lon, correction, fix.roadClass, confidence,
                fix.headingDeg, confidence >= MapMatcher.HEADING_FEEDBACK_MIN_CONFIDENCE,
                MODE_ALONG_ROAD,
            )
        }
    }

    /** Runs on the logger thread; the matcher thread posts its results back here to be written. */
    private fun recordSnap(
        tNs: Long,
        dr: TrackPoint,
        lat: Double,
        lon: Double,
        correctionM: Double,
        roadClass: String,
        confidence: Double,
        roadBearingDeg: Double,
        feedHeadingBack: Boolean,
        mode: String,
    ) {
        snapPoints.add(TrackPoint(lat, lon, GnssQuality.GOOD))
        _snapTrack.value = ArrayList(snapPoints)

        // Close the loop: hand the road's bearing back to the integrator as a heading observation.
        // Position is deliberately not corrected — the snapped track is drawn from the matcher's
        // own output, and teleporting the integrator would destroy the very divergence this app
        // exists to measure.
        //
        // This still runs in along-road mode even though the reported position no longer depends
        // on the integrator's heading, because the walk can lose the network at any point and drop
        // back to dead reckoning; keeping the integrator roughly aligned with the road means that
        // fallback starts from something sane rather than from a heading left over from before.
        var appliedDeg = 0.0
        if (feedHeadingBack) {
            appliedDeg = deadReckoner.applyHeadingCorrection(
                roadBearingDeg, MapMatcher.HEADING_FEEDBACK_GAIN,
            )
        }
        headingCorrectionDeg = deadReckoner.headingCorrectionDeg

        val sb = rowBuilder
        sb.setLength(0)
        sb.append(tNs).append(',')
            .append(dr.lat).append(',').append(dr.lon).append(',')
            .append(lat).append(',').append(lon).append(',')
            .append(correctionM).append(',')
            .append(roadClass).append(',')
            .append(confidence).append(',')
            .append(appliedDeg).append(',')
            .append(mode).append('\n')
        writeRow(matchWriter, sb)
    }

    private fun metresBetween(aLat: Double, aLon: Double, bLat: Double, bLon: Double): Double {
        val mLon = 111_132.0 * kotlin.math.cos(Math.toRadians((aLat + bLat) / 2))
        val dx = (bLon - aLon) * mLon
        val dy = (bLat - aLat) * 111_132.0
        return kotlin.math.sqrt(dx * dx + dy * dy)
    }

    /** Runs on the logger thread during shutdown. */
    private fun closeSession() {
        sessionDir?.let { writeSessionMetadata(it, finished = true) }
        for (w in listOfNotNull(imuWriter, gpsWriter, gnssWriter, gnssRawWriter,
                                gnssNavWriter, drWriter, mlWriter, matchWriter)) {
            try {
                w.flush()
                w.close()
            } catch (e: Exception) {
                Log.e(TAG, "Error closing writer", e)
            }
        }
        imuWriter = null
        gpsWriter = null
        gnssWriter = null
        gnssRawWriter = null
        gnssNavWriter = null
        drWriter = null
        mlWriter = null
        matchWriter = null
    }

    private fun openWriter(dir: File, name: String): BufferedWriter =
        BufferedWriter(
            OutputStreamWriter(FileOutputStream(File(dir, name)), Charsets.UTF_8),
            64 * 1024,
        )

    /** Flush to disk and republish counters, so a crash mid-drive costs at most one interval. */
    private val periodicTask = object : Runnable {
        override fun run() {
            try {
                imuWriter?.flush()
                gpsWriter?.flush()
                gnssWriter?.flush()
                gnssRawWriter?.flush()
                gnssNavWriter?.flush()
                // Cheap, and the reason a session started with Location off recovers
                // even when the PROVIDERS_CHANGED broadcast never arrives.
                ensureLocationSubscribed()
                drWriter?.flush()
                mlWriter?.flush()
                matchWriter?.flush()
            } catch (e: Exception) {
                writeErrors++
                Log.e(TAG, "Flush failed", e)
            }
            publishStatus()
            if (trackDirty) {
                trackDirty = false
                _track.value = ArrayList(trackPoints)
            }
            deadReckoner.position?.let { p ->
                if (drPoints.lastOrNull()?.let { it.lat != p.lat || it.lon != p.lon } != false) {
                    // Mark the stretch where the integrator is running without GNSS help, which
                    // is exactly the stretch whose divergence the demo is about.
                    val unaided = unaidedNow()
                    drPoints.add(
                        p.copy(quality = if (unaided) GnssQuality.LOST else GnssQuality.GOOD)
                    )
                    _drTrack.value = ArrayList(drPoints)

                    // Snap only while the integrator is unaided. With GNSS anchoring every
                    // second there is nothing to correct, and matching a good fix just adds
                    // latency and a chance to snap onto the wrong parallel road.
                    val now = SystemClock.elapsedRealtimeNanos()
                    if (!unaided) {
                        unaidedSinceNs = 0L
                        lastDrLat = Double.NaN
                        if (alongRoadActive) {
                            alongRoadActive = false
                            matchHandler.post { alongRoad.reset() }
                        }
                    } else {
                        if (unaidedSinceNs == 0L) unaidedSinceNs = now
                        val course = deadReckoner.courseDeg
                        val speed = deadReckoner.speed
                        // Modelled uncertainty, not displacement since the anchor. The old
                        // value grew simply because the vehicle drove somewhere, which let
                        // the matcher move an accurate fix onto a neighbouring road.
                        val uncertainty = deadReckoner.positionSigmaM
                        val unaidedS = (now - unaidedSinceNs) / 1e9

                        // The integrator's *distance* is the one channel worth keeping during a
                        // long outage, so that is all the along-road walk is fed. Its heading is
                        // discarded, which is the entire point.
                        val stepM = if (lastDrLat.isNaN()) 0.0
                        else metresBetween(lastDrLat, lastDrLon, p.lat, p.lon)
                        lastDrLat = p.lat
                        lastDrLon = p.lon

                        // The along-road walk is maintained from the first unaided tick but only
                        // reported once displacement is large enough to be worth overriding dead
                        // reckoning; see AlongRoadTracker for why both halves matter.
                        val walk = AlongRoadTracker.enabled
                        // driftMetres, not the sigma: this gate asks "has the vehicle moved far
                        // enough from the anchor for a road walk to beat dead reckoning", which is
                        // a question about displacement, and HANDOVER_DISPLACEMENT_M was tuned
                        // against that quantity. The matcher's budget is a different question and
                        // takes the modelled uncertainty above.
                        val handOver = AlongRoadTracker.shouldHandOver(
                            unaidedS, deadReckoner.driftMetres)
                        matchHandler.post {
                            if (walk) maintainAlongRoad(now, p, course, stepM, handOver)
                            if (!handOver) runMapMatcher(now, p, course, speed, uncertainty)
                        }
                    }
                }
            }
            loggerHandler.postDelayed(this, FLUSH_INTERVAL_MS)
        }
    }

    private fun publishStatus() {
        val now = SystemClock.elapsedRealtimeNanos()
        _status.value = LoggerStatus(
            running = true,
            sessionId = sessionId,
            sessionPath = sessionDir?.absolutePath,
            elapsedSeconds = (now - sessionStartRealtimeNs) / 1_000_000_000L,
            imuSamples = imuSamples,
            gpsFixes = gpsFixes,
            writeErrors = writeErrors,
            lastLat = lastLat,
            lastLon = lastLon,
            lastSpeedMps = lastSpeed,
            lastAccuracyM = lastAccuracy,
            satellitesVisible = satellitesVisible,
            satellitesUsedInFix = satellitesUsedInFix,
            meanCn0DbHz = meanCn0,
            secondsSinceFix =
                if (lastFixRealtimeNs == 0L) -1 else (now - lastFixRealtimeNs) / 1_000_000_000L,
            drLat = deadReckoner.position?.lat,
            drLon = deadReckoner.position?.lon,
            driftMetres = deadReckoner.driftMetres,
            freeRun = freeRunRequested,
            stationary = deadReckoner.isStationary,
            deviceAzimuth = latestAzimuthDeg,
            distanceMetres = distanceMetres,
            snapLat = snapLat,
            snapLon = snapLon,
            snapCorrectionM = snapCorrection,
            snapRoadClass = snapRoadClass,
            snapConfidence = snapConfidence,
            headingCorrectionDeg = headingCorrectionDeg,
            matchMap = matchMapName,
            drSpeedMps = deadReckoner.speed,
            mlMu = mlMu,
            mlStationaryProbability =
                if (mlStationaryLogit.isNaN()) Float.NaN
                else 1f / (1f + kotlin.math.exp(-mlStationaryLogit)),
            mlInferences = mlInferences,
            mlDropped = mlDropped,
        )
    }

    // ------------------------------------------------------------------ sensors

    private fun registerSensors() {
        registeredSensors.clear()
        for (spec in streams) {
            val sensor = sensorManager.getDefaultSensor(spec.type)
            if (sensor == null) {
                Log.w(TAG, "Sensor " + spec.label + " is not present on this device")
                continue
            }
            val ok = sensorManager.registerListener(
                sensorListener,
                sensor,
                spec.periodUs,
                MAX_REPORT_LATENCY_US,
                loggerHandler,
            )
            if (ok) {
                registeredSensors.add(sensor)
            } else {
                Log.w(TAG, "Could not register " + spec.label)
            }
        }
    }

    private val sensorListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val label = sensorTypeLabels[event.sensor.type] ?: return
            val nowNs = event.timestamp
            
            if (calibrationStartTimeNs == 0L) {
                calibrationStartTimeNs = nowNs
            }
            val elapsedSec = (nowNs - calibrationStartTimeNs) / 1_000_000_000.0

            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    lastAccel[0] = event.values[0]
                    lastAccel[1] = event.values[1]
                    lastAccel[2] = event.values[2]
                }
                Sensor.TYPE_GYROSCOPE -> {
                    lastGyro[0] = event.values[0]
                    lastGyro[1] = event.values[1]
                    lastGyro[2] = event.values[2]
                }
                Sensor.TYPE_GRAVITY -> {
                    lastGrav[0] = event.values[0]
                    lastGrav[1] = event.values[1]
                    lastGrav[2] = event.values[2]
                }
            }

            if (elapsedSec < 15.0) {
                // Drop events during the 5s stabilization and 10s orientation calibration phases.
                return
            } else {
                isCalibrating = false
            }

            val sb = rowBuilder
            sb.setLength(0)
            sb.append(event.timestamp).append(',')
                .append(label).append(',')
                .append(event.accuracy)
            val n = if (event.values.size < 6) event.values.size else 6
            for (i in 0 until 6) {
                sb.append(',')
                if (i < n) sb.append(event.values[i])
            }
            sb.append('\n')
            writeRow(imuWriter, sb)
            imuSamples++

            // Drive the strapdown integrator from the same events that reach disk, so replaying
            // the CSV offline reproduces exactly what ran live. Without these three calls the
            // DeadReckoner never integrates and its position is only ever the last GNSS anchor.
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    deadReckoner.onAccel(
                        event.timestamp, event.values[0], event.values[1], event.values[2],
                    )
                    recordDeadReckoning(event.timestamp)
                }
                Sensor.TYPE_GYROSCOPE ->
                    deadReckoner.onGyro(event.values[0], event.values[1], event.values[2])
                Sensor.TYPE_ROTATION_VECTOR ->
                    deadReckoner.onRotationVector(event.values)
            }

            // Levelling for the model runs on the gyro tick because gyro is the fastest stream
            // that also has fresh accelerometer and gravity beside it.
            if (event.sensor.type == Sensor.TYPE_GYROSCOPE) {
                // The rotation vector, not getRotationMatrix(gravity, magnetometer). Two reasons,
                // both measured on 20260904_195146: it tracks GNSS bearing better (10.8 deg RMS
                // against 12.6), and training levels with the rv quaternion, so the magnetometer
                // matrix was a train/serve skew on top of being the worse estimate. It also drops
                // the magnetometer dependency, which is exactly the signal a vehicle corrupts.
                if (!deadReckoner.rotationInto(rotationMatrix)) {
                    return
                }
                SensorManager.getOrientation(rotationMatrix, orientationAngles)
                // Cached rather than published here: this runs at 200 Hz, and writing the status
                // StateFlow per gyro event allocates a LoggerStatus and wakes the UI collector
                // 200 times a second. The periodic tick publishes it instead.
                latestAzimuthDeg = Math.toDegrees(orientationAngles[0].toDouble()).toFloat()

                if (!modelReady) return

                val now = event.timestamp
                if (now - lastMLFeedTimeNanos < ML_PERIOD_NS) return
                lastMLFeedTimeNanos = now

                // Gravity-cancelled acceleration, rotated into the earth frame the model was
                // trained in. NOTE: training used the fused linear_accel stream rotated by the
                // rotation-vector quaternion, not this magnetometer-derived matrix — see the
                // train/serve skew note in README.
                val linAccX = lastAccel[0] - lastGrav[0]
                val linAccY = lastAccel[1] - lastGrav[1]
                val linAccZ = lastAccel[2] - lastGrav[2]
                val earthAccX = rotationMatrix[0] * linAccX + rotationMatrix[1] * linAccY + rotationMatrix[2] * linAccZ
                val earthAccY = rotationMatrix[3] * linAccX + rotationMatrix[4] * linAccY + rotationMatrix[5] * linAccZ
                val earthAccZ = rotationMatrix[6] * linAccX + rotationMatrix[7] * linAccY + rotationMatrix[8] * linAccZ

                // Debiased, because the checkpoint is trained on features built with
                // `--debias all`: the per-run stationary offset is subtracted from all six
                // channels before windowing. Feeding the raw channel here would be a
                // train/serve skew on exactly the signal that matters most - the gyro offset
                // is small per sample but heading is its integral, and removing it took
                // free-running drift from 51.8% to 19.3% on the held-out runs.
                //
                // Before the first stop the bias is still zero, which is the honest answer: a
                // guess would be worse than none, and DeadReckoner.gyroBiasValid records which
                // regime a session was in.
                deadReckoner.debiasGyro(gyroForModel, lastGyro[0], lastGyro[1], lastGyro[2])
                submitToModel(
                    now, earthAccX, earthAccY, earthAccZ,
                    gyroForModel[0], gyroForModel[1], gyroForModel[2],
                )
            }
        }

        override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {
            // Captured per-sample in the accuracy column instead.
        }
    }

    // ------------------------------------------------------------------ location

    /**
     * Whether the location subscriptions are currently live.
     *
     * Subscribing is not a one-off. Starting a session with Location switched off used to leave
     * the recording permanently blind: `registerGnssStatusCallback` returns false, the raw
     * callbacks refuse, and the fused client has nothing to deliver — and none of it was ever
     * retried, so turning Location on ten seconds later produced no fixes for the rest of the
     * drive. This flag plus [ensureLocationSubscribed] makes subscription a state to be
     * maintained rather than an event that happened once.
     */
    private var locationSubscribed = false

    /** True while the OS reports at least one usable location provider. */
    private fun locationEnabled(): Boolean = try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            locationManager.isLocationEnabled
        } else {
            locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }
    } catch (e: Exception) {
        Log.w(TAG, "Could not read location state", e)
        false
    }

    /**
     * Bring the location subscriptions into line with whether Location is switched on.
     *
     * Idempotent and cheap, so it can be called from the settings broadcast AND from the two
     * second flush tick. Both, deliberately: the broadcast is the responsive path, but several
     * OEM builds — Samsung among them — throttle or drop implicit broadcasts to background
     * processes, and a recording that silently never recovers is exactly the failure this is
     * fixing. The poll costs one boolean read per two seconds and cannot be throttled away.
     */
    @SuppressLint("MissingPermission") // guarded by hasLocationPermission() before the session starts
    private fun ensureLocationSubscribed() {
        if (!sessionActive) return
        if (!locationEnabled()) {
            // Drop the subscriptions rather than leaving stale ones attached, so that when
            // Location comes back the re-subscribe below runs from a known state.
            if (locationSubscribed) {
                Log.i(TAG, "Location switched off; releasing subscriptions")
                teardownLocation()
                locationSubscribed = false
            }
            return
        }
        if (locationSubscribed) return
        if (!hasLocationPermission()) return

        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, 1_000)
            .setMinUpdateIntervalMillis(500)
            .setWaitForAccurateLocation(false)
            .build()
        fusedLocationClient.requestLocationUpdates(request, locationCallback, loggerThread.looper)
        registerGnssStatus()
        locationSubscribed = true
        locationResubscribes++
        Log.i(TAG, "Location subscriptions live (attempt $locationResubscribes)")
    }

    /** Release every location subscription. Safe to call when nothing is registered. */
    private fun teardownLocation() {
        fusedLocationClient.removeLocationUpdates(locationCallback)
        for (unregister in listOf<() -> Unit>(
            { locationManager.unregisterGnssStatusCallback(gnssCallback) },
            { locationManager.unregisterGnssMeasurementsCallback(measurementsCallback) },
            { locationManager.unregisterGnssNavigationMessageCallback(navMessageCallback) },
        )) {
            try {
                unregister()
            } catch (e: Exception) {
                Log.w(TAG, "Location callback was not registered", e)
            }
        }
    }

    /**
     * Watches the system Location toggle so a session started with it off recovers immediately.
     *
     * PROVIDERS_CHANGED is sent when the master switch or an individual provider changes. It is
     * the fast path; [ensureLocationSubscribed] is also polled, because this broadcast is not
     * reliably delivered on every OEM build.
     */
    private fun registerProviderWatcher() {
        if (providerWatcherRegistered) return
        val filter = IntentFilter(LocationManager.PROVIDERS_CHANGED_ACTION).apply {
            addAction(LocationManager.MODE_CHANGED_ACTION)
        }
        ContextCompat.registerReceiver(
            this, providerWatcher, filter, ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        providerWatcherRegistered = true
    }

    private var providerWatcherRegistered = false

    /** Counts successful (re)subscriptions, so session.json shows whether recovery happened. */
    private var locationResubscribes = 0

    private val providerWatcher = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            // Hop to the logger thread: everything that touches the writers and counters runs
            // there, and a broadcast arrives on the main thread.
            loggerHandler.post { ensureLocationSubscribed() }
        }
    }

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            for (location in result.locations) logLocation(location)
        }
    }

    private fun logLocation(location: Location) {
        val sb = rowBuilder
        sb.setLength(0)
        // elapsedRealtimeNanos puts the fix on the same clock as the IMU samples, which is what
        // makes the two streams alignable without guessing an offset.
        sb.append(location.elapsedRealtimeNanos).append(',')
            .append(location.time).append(',')
            .append(location.provider ?: "").append(',')
            .append(location.latitude).append(',')
            .append(location.longitude).append(',')
        if (location.hasAltitude()) sb.append(location.altitude)
        sb.append(',')
        if (location.hasSpeed()) sb.append(location.speed)
        sb.append(',')
        if (location.hasBearing()) sb.append(location.bearing)
        sb.append(',')
        if (location.hasAccuracy()) sb.append(location.accuracy)
        sb.append(',')
        if (location.hasVerticalAccuracy()) sb.append(location.verticalAccuracyMeters)
        sb.append(',')
        if (location.hasSpeedAccuracy()) sb.append(location.speedAccuracyMetersPerSecond)
        sb.append(',')
        if (location.hasBearingAccuracy()) sb.append(location.bearingAccuracyDegrees)
        sb.append('\n')
        writeRow(gpsWriter, sb)

        gpsFixes++
        lastFixRealtimeNs = location.elapsedRealtimeNanos
        lastLat = location.latitude
        lastLon = location.longitude
        lastSpeed = if (location.hasSpeed()) location.speed else null
        lastBearing = if (location.hasBearing()) location.bearing else null
        lastAccuracy = if (location.hasAccuracy()) location.accuracy else null

        // Accumulate ground truth distance. Gated on speed because GNSS jitter while parked
        // otherwise inflates the denominator by metres a minute and flatters the drift ratio.
        val movingFastEnough = location.hasSpeed() && location.speed > 0.5f
        if (movingFastEnough && !lastDistanceLat.isNaN()) {
            val results = FloatArray(1)
            Location.distanceBetween(
                lastDistanceLat, lastDistanceLon, location.latitude, location.longitude, results,
            )
            distanceMetres += results[0]
        }
        if (movingFastEnough || lastDistanceLat.isNaN()) {
            lastDistanceLat = location.latitude
            lastDistanceLon = location.longitude
        }

        // Quality is captured per fix so the map can shade the stretch where GNSS was failing.
        val quality = when {
            satellitesUsedInFix < 4 || meanCn0 < 20f -> GnssQuality.WEAK
            else -> GnssQuality.GOOD
        }
        trackPoints.add(TrackPoint(location.latitude, location.longitude, quality))
        trackDirty = true

        // Only healthy fixes are allowed to correct the integrator. Anchoring to a degraded fix
        // would hide exactly the error this app exists to measure — and in free-run mode nothing
        // corrects it at all.
        val healthy = satellitesUsedInFix >= 4 &&
            (!location.hasAccuracy() || location.accuracy < 20f)
        if (!freeRunRequested && healthy) {
            deadReckoner.anchorTo(
                location.latitude,
                location.longitude,
                if (location.hasSpeed()) location.speed else null,
                if (location.hasBearing()) location.bearing else null,
            )
        } else if (!deadReckoner.initialised) {
            // Free-run cannot start from nothing; the first fix always seeds the origin.
            deadReckoner.anchorTo(location.latitude, location.longitude, null, null)
        }
    }

    /** Append a dead-reckoning row at 10 Hz — enough to plot, far below the 200 Hz update rate. */
    private fun recordDeadReckoning(tNs: Long) {
        if (tNs - lastDrSampleNs < 100_000_000L) return
        lastDrSampleNs = tNs
        val p = deadReckoner.position ?: return
        val b = deadReckoner.bias()
        val g = deadReckoner.gyroBias
        val sb = rowBuilder
        sb.setLength(0)
        sb.append(tNs).append(',')
            .append(p.lat).append(',')
            .append(p.lon).append(',')
            .append(deadReckoner.speed).append(',')
            .append(deadReckoner.driftMetres).append(',')
            .append(b[0]).append(',').append(b[1]).append(',').append(b[2]).append(',')
            .append(if (deadReckoner.isStationary) 1 else 0).append(',')
            .append(if (freeRunRequested) 1 else 0).append(',')
            .append(g[0]).append(',').append(g[1]).append(',').append(g[2]).append(',')
            .append(if (deadReckoner.gyroBiasValid) 1 else 0).append('\n')
        writeRow(drWriter, sb)
    }

    @SuppressLint("MissingPermission")
    private fun registerGnssStatus() {
        locationManager.registerGnssStatusCallback(gnssCallback, loggerHandler)

        // Raw measurements are not guaranteed. Every Android device since API 24 exposes the
        // API, but whether the chipset actually delivers anything is a per-device decision, and
        // some vendors return an empty stream rather than failing. The registration result is
        // recorded either way so a session with no gnss_raw.csv rows can be told apart from a
        // session where the callback was never accepted.
        gnssRawSupported = try {
            locationManager.registerGnssMeasurementsCallback(measurementsCallback, loggerHandler)
        } catch (e: Exception) {
            Log.w(TAG, "Raw GNSS measurements unavailable", e)
            false
        }
        gnssNavSupported = try {
            locationManager.registerGnssNavigationMessageCallback(navMessageCallback, loggerHandler)
        } catch (e: Exception) {
            Log.w(TAG, "GNSS navigation messages unavailable", e)
            false
        }
        Log.i(TAG, "Raw GNSS: measurements=$gnssRawSupported nav=$gnssNavSupported")
    }

    /**
     * Per-satellite observables: Doppler, code phase, carrier phase and the receiver clock.
     *
     * Logging only. Nothing in the navigation path reads this yet - the sub-four-satellite
     * velocity solution it exists to feed is a draft in eval/scarce_gnss.py, and the whole
     * reason for recording is that the draft has only ever been tested against a simulated
     * constellation.
     */
    private val measurementsCallback = object : GnssMeasurementsEvent.Callback() {
        override fun onGnssMeasurementsReceived(event: GnssMeasurementsEvent) {
            val t = SystemClock.elapsedRealtimeNanos()
            val c = event.clock
            // Absent fields are written empty rather than as a sentinel: 0 is a legal value for
            // most of these, so a sentinel would be indistinguishable from a real reading.
            val fullBias = if (c.hasFullBiasNanos()) c.fullBiasNanos.toString() else ""
            val bias = if (c.hasBiasNanos()) c.biasNanos.toString() else ""
            val drift = if (c.hasDriftNanosPerSecond()) c.driftNanosPerSecond.toString() else ""
            val driftUnc = if (c.hasDriftUncertaintyNanosPerSecond())
                c.driftUncertaintyNanosPerSecond.toString() else ""
            val leap = if (c.hasLeapSecond()) c.leapSecond.toString() else ""

            for (m in event.measurements) {
                val sb = rowBuilder
                sb.setLength(0)
                sb.append(t).append(',')
                    .append(c.timeNanos).append(',')
                    .append(fullBias).append(',')
                    .append(bias).append(',')
                    .append(drift).append(',')
                    .append(driftUnc).append(',')
                    .append(c.hardwareClockDiscontinuityCount).append(',')
                    .append(leap).append(',')
                    .append(m.svid).append(',')
                    .append(m.constellationType).append(',')
                    .append(m.state).append(',')
                    .append(m.receivedSvTimeNanos).append(',')
                    .append(m.receivedSvTimeUncertaintyNanos).append(',')
                    .append(m.cn0DbHz).append(',')
                    .append(m.pseudorangeRateMetersPerSecond).append(',')
                    .append(m.pseudorangeRateUncertaintyMetersPerSecond).append(',')
                    .append(m.accumulatedDeltaRangeMeters).append(',')
                    .append(m.accumulatedDeltaRangeUncertaintyMeters).append(',')
                    .append(m.accumulatedDeltaRangeState).append(',')
                    .append(if (m.hasCarrierFrequencyHz()) m.carrierFrequencyHz.toString() else "")
                    .append(',')
                    .append(m.multipathIndicator).append('\n')
                writeRow(gnssRawWriter, sb)
                gnssRawRows++
            }
        }

        override fun onStatusChanged(status: Int) {
            Log.i(TAG, "GNSS measurements status $status")
        }
    }

    /**
     * Broadcast ephemeris, kept as raw subframe bytes.
     *
     * Doppler alone does not give velocity: equation (2) in eval/scarce_gnss.py needs each
     * satellite's own position and velocity to subtract, and those come from the ephemeris.
     * Decoding subframes on the phone would be work with no on-device consumer, so the bytes
     * are stored base64 and decoded offline.
     */
    private val navMessageCallback = object : GnssNavigationMessage.Callback() {
        override fun onGnssNavigationMessageReceived(msg: GnssNavigationMessage) {
            val sb = rowBuilder
            sb.setLength(0)
            sb.append(SystemClock.elapsedRealtimeNanos()).append(',')
                .append(msg.type).append(',')
                .append(msg.svid).append(',')
                .append(msg.messageId).append(',')
                .append(msg.submessageId).append(',')
                .append(msg.status).append(',')
                .append(Base64.encodeToString(msg.data, Base64.NO_WRAP)).append('\n')
            writeRow(gnssNavWriter, sb)
            gnssNavRows++
        }

        override fun onStatusChanged(status: Int) {
            Log.i(TAG, "GNSS navigation message status $status")
        }
    }

    /**
     * Raw constellation health. Satellite count and C/N0 collapse before the fused provider stops
     * emitting fixes, so this is the earliest evidence available that the vehicle is entering a
     * tunnel, and the natural input to a GPS-versus-IMU trust weighting.
     */
    private val gnssCallback = object : GnssStatus.Callback() {
        override fun onSatelliteStatusChanged(gnssStatus: GnssStatus) {
            var used = 0
            var sumCn0 = 0f
            var maxCn0 = 0f
            for (i in 0 until gnssStatus.satelliteCount) {
                val cn0 = gnssStatus.getCn0DbHz(i)
                if (cn0 > maxCn0) maxCn0 = cn0
                if (gnssStatus.usedInFix(i)) {
                    used++
                    sumCn0 += cn0
                }
            }
            satellitesVisible = gnssStatus.satelliteCount
            satellitesUsedInFix = used
            meanCn0 = if (used > 0) sumCn0 / used else 0f

            val sb = rowBuilder
            sb.setLength(0)
            sb.append(SystemClock.elapsedRealtimeNanos()).append(',')
                .append(satellitesVisible).append(',')
                .append(used).append(',')
                .append(meanCn0).append(',')
                .append(maxCn0).append('\n')
            writeRow(gnssWriter, sb)
        }
    }

    // ------------------------------------------------------------------ io helpers

    private fun writeRow(writer: BufferedWriter?, row: CharSequence) {
        try {
            writer?.append(row)
        } catch (e: Exception) {
            writeErrors++
            if (writeErrors == 1L) Log.e(TAG, "Write failed", e)
        }
    }

    /**
     * Sidecar describing the device, the exact sensors that were registered, and the mapping
     * between the monotonic timebase used in the CSVs and UTC. Without the sensor inventory a
     * trace cannot be reproduced or compared across phones.
     */
    private fun writeSessionMetadata(dir: File, finished: Boolean = false) {
        try {
            val root = JSONObject()
            root.put("session_id", sessionId)
            root.put("app_version", BuildConfig.VERSION_NAME)
            root.put(
                "device",
                JSONObject()
                    .put("manufacturer", Build.MANUFACTURER)
                    .put("model", Build.MODEL)
                    .put("device", Build.DEVICE)
                    .put("android_release", Build.VERSION.RELEASE)
                    .put("sdk_int", Build.VERSION.SDK_INT),
            )
            root.put("timebase", "SystemClock.elapsedRealtimeNanos")
            root.put(
                "clock_sync",
                JSONObject()
                    .put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos())
                    .put("unix_epoch_ms", System.currentTimeMillis()),
            )
            root.put("session_start_elapsed_realtime_ns", sessionStartRealtimeNs)

            val sensorArray = JSONArray()
            for (s in registeredSensors) {
                sensorArray.put(
                    JSONObject()
                        .put("label", sensorTypeLabels[s.type])
                        .put("name", s.name)
                        .put("vendor", s.vendor)
                        .put("type", s.type)
                        .put("max_range", s.maximumRange.toDouble())
                        .put("resolution", s.resolution.toDouble())
                        .put("power_ma", s.power.toDouble())
                        .put("min_delay_us", s.minDelay)
                        .put("max_delay_us", s.maxDelay)
                        .put("fifo_max_events", s.fifoMaxEventCount)
                        .put("is_wake_up", s.isWakeUpSensor),
                )
            }
            root.put("sensors", sensorArray)

            if (finished) {
                root.put(
                    "summary",
                    JSONObject()
                        .put("imu_samples", imuSamples)
                        .put("gps_fixes", gpsFixes)
                        .put("write_errors", writeErrors)
                        // Supported-but-silent and never-registered look identical from the CSV
                        // alone, and they mean different things: one is a chipset that withholds
                        // raw data, the other is a bug. Both are recorded.
                        .put("gnss_raw_supported", gnssRawSupported)
                        .put("gnss_raw_rows", gnssRawRows)
                        .put("gnss_nav_supported", gnssNavSupported)
                        .put("gnss_nav_rows", gnssNavRows)
                        .put("location_subscribes", locationResubscribes)
                        .put(
                            "duration_s",
                            (SystemClock.elapsedRealtimeNanos() - sessionStartRealtimeNs) / 1e9,
                        ),
                )
            }

            File(dir, "session.json").writeText(root.toString(2))
        } catch (e: Exception) {
            Log.e(TAG, "Could not write session metadata", e)
        }
    }

    // ------------------------------------------------------------------ plumbing

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ImuLogger:session")
        lock.setReferenceCounted(false)
        // Without this the CPU suspends once the screen goes off and sensor delivery becomes
        // bursty and lossy. The timeout is a safety valve against a leaked lock.
        lock.acquire(WAKELOCK_TIMEOUT_MS)
        wakeLock = lock
    }

    private fun fail(message: String) {
        sessionActive = false
        _status.value = LoggerStatus(running = false, error = message)
        // Stopping well inside the five-second startForeground window keeps the system from
        // raising ForegroundServiceDidNotStartInTimeException on the way out.
        stopSelf()
    }

    private fun startForegroundWithType() {
        createNotificationChannel()
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            buildNotification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION,
        )
    }

    private fun buildNotification(): Notification {
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            flags,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, SensorService::class.java).setAction(ACTION_STOP),
            flags,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.channel_description))
            .setSmallIcon(R.drawable.ic_stat_sensors)
            .setContentIntent(open)
            .addAction(0, getString(R.string.notif_stop), stop)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        )
        channel.description = getString(R.string.channel_description)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }
}
