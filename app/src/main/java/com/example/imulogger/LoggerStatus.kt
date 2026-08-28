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
) {
    /**
     * How much the fusion stage should trust GNSS right now. Derived here rather than in the UI so
     * the filter and the display can never disagree about what the constellation is doing.
     */
    val gnssQuality: GnssQuality
        get() = when {
            !running -> GnssQuality.IDLE
            secondsSinceFix < 0 || secondsSinceFix > 5 -> GnssQuality.LOST
            satellitesUsedInFix < 4 || meanCn0DbHz < 20f -> GnssQuality.WEAK
            else -> GnssQuality.GOOD
        }
}

enum class GnssQuality { IDLE, GOOD, WEAK, LOST }

/** One recorded fix, kept in memory for the map track. */
data class TrackPoint(val lat: Double, val lon: Double)
