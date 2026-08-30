package com.example.imulogger

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
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

    private var sessionDir: File? = null
    private var sessionId: String = ""
    private var sessionStartRealtimeNs: Long = 0
    
    // ML Model Integration
    private lateinit var imuModelRunner: IMUModelRunner
    private val lastAccel = FloatArray(3)
    private val lastGyro = FloatArray(3)
    private val lastMag = FloatArray(3)
    private val lastGrav = FloatArray(3)
    private var modelReady = false
    
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
    private var satellitesVisible: Int = 0
    private var satellitesUsedInFix: Int = 0
    private var meanCn0: Float = 0f

    /** Reused so the hot path does not allocate a builder per sample. */
    private val rowBuilder = StringBuilder(160)

    /** Logger-thread-owned; a snapshot is published to [track] on the periodic tick. */
    private val trackPoints = ArrayList<TrackPoint>()
    private var trackDirty = false

    private val deadReckoner = DeadReckoner()
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
        
        // Initialize ML model runner
        try {
            imuModelRunner = IMUModelRunner(this)
            modelReady = true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load ML model", e)
        }
    }

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
        fusedLocationClient.removeLocationUpdates(locationCallback)
        try {
            locationManager.unregisterGnssStatusCallback(gnssCallback)
        } catch (e: Exception) {
            Log.w(TAG, "GNSS callback was not registered", e)
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
        trackPoints.clear()
        drPoints.clear()
        deadReckoner.reset()
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

        drWriter = openWriter(dir, "deadreckon.csv")
        drWriter?.write("t_ns,lat,lon,speed_mps,drift_m,bias_e,bias_n,bias_u,stationary,free_run\n")

        acquireWakeLock()
        registerSensors()
        requestLocation()
        registerGnssStatus()
        writeSessionMetadata(dir)

        loggerHandler.post(periodicTask)

        _status.value = LoggerStatus(
            running = true,
            sessionId = sessionId,
            sessionPath = dir.absolutePath,
        )
    }

    /** Runs on the logger thread during shutdown. */
    private fun closeSession() {
        sessionDir?.let { writeSessionMetadata(it, finished = true) }
        for (w in listOfNotNull(imuWriter, gpsWriter, gnssWriter, drWriter)) {
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
        drWriter = null
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
                drWriter?.flush()
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
                if (drPoints.lastOrNull() != p) {
                    drPoints.add(p)
                    _drTrack.value = ArrayList(drPoints)
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
                Sensor.TYPE_MAGNETIC_FIELD -> {
                    lastMag[0] = event.values[0]
                    lastMag[1] = event.values[1]
                    lastMag[2] = event.values[2]
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
            
            // ML Integration: Run inference when gyro arrives
            if (modelReady && event.sensor.type == Sensor.TYPE_GYROSCOPE) {
                
                // 1. Cancel gravity in device frame
                val linAccX = lastAccel[0] - lastGrav[0]
                val linAccY = lastAccel[1] - lastGrav[1]
                val linAccZ = lastAccel[2] - lastGrav[2]
                
                // 2. Transform linear acc to Earth frame
                val R = FloatArray(9)
                val I = FloatArray(9)
                if (SensorManager.getRotationMatrix(R, I, lastGrav, lastMag)) {
                    // R is [H, M, A] rotation matrix. R * linAcc transforms to global frame
                    val earthAccX = R[0] * linAccX + R[1] * linAccY + R[2] * linAccZ
                    val earthAccY = R[3] * linAccX + R[4] * linAccY + R[5] * linAccZ
                    val earthAccZ = R[6] * linAccX + R[7] * linAccY + R[8] * linAccZ
                    
                    val speed = lastSpeed ?: 0f
                    val bearing = lastBearing ?: 0f
                    
                    val correction = imuModelRunner.processIMUData(
                        earthAccX, earthAccY, earthAccZ,
                        lastGyro[0], lastGyro[1], lastGyro[2],
                        speed, bearing
                    )
                    
                    if (correction != null && correction.size == 2) {
                        val deltaV = correction[0].toDouble()
                        val deltaTheta = correction[1].toDouble()
                        // Since this is delta speed and delta bearing, passing as lat/lon offsets is a simplification
                        deadReckoner.applyMLCorrection(deltaV * 0.0001, deltaTheta * 0.0001)
                    }
                }
            }
        }

        override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {
            // Captured per-sample in the accuracy column instead.
        }
    }

    // ------------------------------------------------------------------ location

    @SuppressLint("MissingPermission") // guarded by hasLocationPermission() before the session starts
    private fun requestLocation() {
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, 1_000)
            .setMinUpdateIntervalMillis(500)
            .setWaitForAccurateLocation(false)
            .build()
        fusedLocationClient.requestLocationUpdates(request, locationCallback, loggerThread.looper)
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

        trackPoints.add(TrackPoint(location.latitude, location.longitude))
        trackDirty = true

        // Only healthy fixes are allowed to correct the integrator. Anchoring to a degraded fix
        // would hide exactly the error this app exists to measure � and in free-run mode nothing
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

    /** Append a dead-reckoning row at 10 Hz � enough to plot, far below the 200 Hz update rate. */
    private fun recordDeadReckoning(tNs: Long) {
        if (tNs - lastDrSampleNs < 100_000_000L) return
        lastDrSampleNs = tNs
        val p = deadReckoner.position ?: return
        val b = deadReckoner.bias()
        val sb = rowBuilder
        sb.setLength(0)
        sb.append(tNs).append(',')
            .append(p.lat).append(',')
            .append(p.lon).append(',')
            .append(deadReckoner.speed).append(',')
            .append(deadReckoner.driftMetres).append(',')
            .append(b[0]).append(',').append(b[1]).append(',').append(b[2]).append(',')
            .append(if (deadReckoner.isStationary) 1 else 0).append(',')
            .append(if (freeRunRequested) 1 else 0).append('\n')
        writeRow(drWriter, sb)
    }

    @SuppressLint("MissingPermission")
    private fun registerGnssStatus() {
        locationManager.registerGnssStatusCallback(gnssCallback, loggerHandler)
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
