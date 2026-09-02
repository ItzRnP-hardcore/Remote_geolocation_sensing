package com.example.imulogger

/**
 * Immutable snapshot of the recording session, published by [SensorService] for the UI.
 *
 * Everything here is derived on the logger thread and republished at a low, fixed rate so the
 * UI never sees a partially updated set of counters.
 */
data class LoggerStatus(
    val running: Boolean = false,
    val sessionId: String? = null,
    val sessionPath: String? = null,
    val error: String? = null,

    /** Wall-clock seconds since the session started. */
    val elapsedSeconds: Long = 0,

    val imuSamples: Long = 0,
    val gpsFixes: Long = 0,
    val writeErrors: Long = 0,

    val lastLat: Double? = null,
    val lastLon: Double? = null,
    val lastSpeedMps: Float? = null,
    val lastAccuracyM: Float? = null,

    val satellitesVisible: Int = 0,
    val satellitesUsedInFix: Int = 0,
    val meanCn0DbHz: Float = 0f,

    /**
     * Seconds since the last GPS fix. This is the signal the fusion stage will use to decide when
     * to lean on the IMU instead of GNSS (tunnel, underpass, urban canyon).
     */
    val secondsSinceFix: Long = 0,

    /** Dead-reckoned position, integrated from the IMU alone since the last GNSS anchor. */
    val drLat: Double? = null,
    val drLon: Double? = null,

    /** Metres between the dead-reckoned position and the last trusted GNSS anchor. */
    val driftMetres: Double = 0.0,

    /** True when GNSS is being withheld from the integrator on purpose, to emulate a tunnel. */
    val freeRun: Boolean = false,

    /** The integrator believes the vehicle is stopped, so a zero-velocity update is being applied. */
    val stationary: Boolean = false,

    /** Current orientation of the device in degrees (compass heading) */
    val deviceAzimuth: Float? = null,

    /**
     * Latest model outputs, for observation only. Nothing in the navigation path consumes these
     * yet: mu is trained against speed in m/s over a 10 s window, and until that is validated
     * against GNSS ground truth it should not be allowed to move the position estimate.
     */
    val mlMu: Float = Float.NaN,
    val mlStationaryProbability: Float = Float.NaN,
    val mlInferences: Long = 0,
    val mlDropped: Long = 0,

    /** Map-matched position: the dead-reckoned fix snapped onto the road network. */
    val snapLat: Double? = null,
    val snapLon: Double? = null,

    /** Metres the matcher moved the fix to put it on a road. */
    val snapCorrectionM: Double = 0.0,
    val snapRoadClass: String? = null,
    val snapConfidence: Double = 0.0,

    /** Cumulative degrees the map has rotated the integrator's course this session. */
    val headingCorrectionDeg: Double = 0.0,

    /** Ground distance covered so far, from GNSS. The denominator of the benchmark. */
    val distanceMetres: Double = 0.0,

    /** Speed the integrator believes it is doing, for comparison against [lastSpeedMps]. */
    val drSpeedMps: Double = 0.0,
) {
    /**
     * How much the fusion stage should trust GNSS right now. Derived here rather than in the UI so
     * the filter and the display can never disagree about what the constellation is doing.
     */
    /**
     * Drift as a fraction of distance travelled — the number the execution plan actually sets a
     * target for (< 10%). A bare metre count cannot be judged without knowing how far we went.
     * Null until enough ground has been covered for the ratio to mean anything.
     */
    val driftPercent: Double?
        get() = if (distanceMetres < MIN_DISTANCE_FOR_PERCENT) null
        else 100.0 * driftMetres / distanceMetres

    val gnssQuality: GnssQuality
        get() = when {
            !running -> GnssQuality.IDLE
            secondsSinceFix < 0 || secondsSinceFix > 5 -> GnssQuality.LOST
            satellitesUsedInFix < 4 || meanCn0DbHz < 20f -> GnssQuality.WEAK
            else -> GnssQuality.GOOD
        }
}

/** Below this the drift ratio is dominated by GNSS noise rather than by real error. */
const val MIN_DISTANCE_FOR_PERCENT = 50.0

/** The execution plan's target: drift strictly under this share of distance travelled. */
const val DRIFT_BENCHMARK_PERCENT = 10.0

enum class GnssQuality { IDLE, GOOD, WEAK, LOST }

/**
 * One recorded fix, kept in memory for the map track.
 *
 * [quality] travels with the point so the map can colour the stretch where the constellation was
 * failing. Without it the divergence between the two tracks has no visible cause.
 */
data class TrackPoint(
    val lat: Double,
    val lon: Double,
    val quality: GnssQuality = GnssQuality.GOOD,
)
