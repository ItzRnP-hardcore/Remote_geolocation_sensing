package com.example.imulogger

import android.hardware.SensorManager
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sqrt

/**
 * Strapdown inertial dead reckoning, run alongside GNSS so the two can be compared.
 *
 * The point of this class is not to be a good navigator on its own — nothing that integrates a
 * consumer MEMS accelerometer twice is — it is to make the error *visible*. It tracks a position
 * that is anchored to GNSS while the constellation is healthy and free-runs on the IMU alone when
 * it is not, so the gap between [position] and the GPS track is a direct read of what a tunnel
 * would cost you.
 *
 * Frame convention: ENU (x east, y north, z up), matching what
 * [SensorManager.getRotationMatrixFromVector] produces. All internal state is metres and m/s in a
 * local tangent plane anchored at [originLat]/[originLon]; the conversion back to degrees happens
 * only when a caller asks for [position].
 *
 * Error handling is deliberate rather than statistical: there is no covariance here. This is the
 * measurement rig a Kalman filter gets tuned against, not the filter itself.
 */
class DeadReckoner {

    private companion object {
        /** Standard gravity, used only until the stationary estimator learns the real local value. */
        const val G_NOMINAL = 9.80665

        /** How fast the learned gravity magnitude tracks the stationary accelerometer norm. */
        const val K_GRAVITY = 0.02

        /** How fast the world-frame accelerometer bias is learned during stand-still. */
        const val K_BIAS = 0.02

        /** Stationarity gates: |‖a‖ − g| and ‖ω‖ must both stay under these. */
        const val STILL_ACCEL_TOL = 0.15
        const val STILL_GYRO_TOL = 0.05
        const val STILL_HOLD_S = 0.5

        /** A dt larger than this means samples were dropped; integrating across it invents motion. */
        const val MAX_DT_S = 0.05

        /** Below this, the velocity vector has no meaningful direction to correct. */
        const val MIN_SPEED_FOR_HEADING_FIX = 2.0

        /**
         * Most the map may rotate the course in one update, degrees.
         *
         * This is the safety valve on a feedback loop: the matcher feeds heading back into the
         * integrator, so one confident match onto the wrong road would otherwise swing the
         * estimate straight onto it and then keep it there. Capping the step means a wrong road
         * has to be believed repeatedly before it can do real damage, and a right one still pulls
         * the heading in within a few seconds.
         */
        const val MAX_HEADING_STEP_DEG = 4.0
    }

    /** Position in the local tangent plane, metres east/north of the origin. */
    private var pE = 0.0
    private var pN = 0.0
    private var pU = 0.0

    private var vE = 0.0
    private var vN = 0.0
    private var vU = 0.0

    /** World-frame accelerometer bias, learned during stand-still. */
    private var bE = 0.0
    private var bN = 0.0
    private var bU = 0.0

    private var gravity = G_NOMINAL
    private var stillFor = 0.0
    private var lastGyroNorm = 0.0
    private var lastAccelNs = 0L

    private val rotation = FloatArray(9)
    private var haveRotation = false

    private var originLat = Double.NaN
    private var originLon = Double.NaN
    private var mPerDegLat = 111_132.0
    private var mPerDegLon = 111_320.0

    /** True once a GNSS fix has anchored the origin; before that there is nothing to integrate from. */
    val initialised: Boolean get() = !originLat.isNaN()

    var isStationary: Boolean = false
        private set

    /** Metres of drift accumulated since the last GNSS anchor. */
    var driftMetres: Double = 0.0
        private set

    private var anchorE = 0.0
    private var anchorN = 0.0

    /** Current dead-reckoned position, or null before the first GNSS anchor. */
    val position: TrackPoint?
        get() = if (!initialised) null
        else TrackPoint(originLat + pN / mPerDegLat, originLon + pE / mPerDegLon)

    val speed: Double get() = sqrt(vE * vE + vN * vN)

    /**
     * Direction of travel in degrees clockwise from north, or null when too slow for the
     * direction of the velocity vector to mean anything. This is the quantity the map matcher
     * compares against road bearing, and the quantity dead reckoning is worst at.
     */
    val courseDeg: Double?
        get() {
            if (speed < 0.3) return null
            val deg = Math.toDegrees(kotlin.math.atan2(vE, vN))
            return if (deg < 0) deg + 360 else deg
        }

    fun bias(): DoubleArray = doubleArrayOf(bE, bN, bU)

    /** Total degrees the map has rotated the course this session, for observability. */
    var headingCorrectionDeg: Double = 0.0
        private set

    /**
     * Rotate the course toward a road bearing, keeping speed untouched.
     *
     * This is the whole point of map matching for this project. Integrated speed is competitive —
     * measured at 3.10 m/s against GPS's 3.36 on a recorded run — while heading is where the
     * error lives, and a road's bearing is an observation of exactly the quantity the IMU cannot
     * recover for itself. Speed is deliberately left alone: correcting a channel that is already
     * good would only inject the map's error into it.
     *
     * The road direction is ambiguous on a two-way road, so the nearer of the two is taken;
     * [gain] scales how much of the disagreement is absorbed per update. Returns the degrees
     * actually applied.
     */
    fun applyHeadingCorrection(roadBearingDeg: Double, gain: Double): Double {
        val sp = speed
        if (sp < MIN_SPEED_FOR_HEADING_FIX) return 0.0

        val course = Math.toDegrees(kotlin.math.atan2(vE, vN))
        // A road carries traffic both ways; the direction we are actually going is the one that
        // disagrees least with where we already think we are heading.
        val forward = signedDelta(roadBearingDeg, course)
        val backward = signedDelta(roadBearingDeg + 180.0, course)
        val delta = if (abs(forward) <= abs(backward)) forward else backward

        val applied = (gain * delta).coerceIn(-MAX_HEADING_STEP_DEG, MAX_HEADING_STEP_DEG)
        val corrected = Math.toRadians(course + applied)
        vE = sp * kotlin.math.sin(corrected)
        vN = sp * kotlin.math.cos(corrected)
        headingCorrectionDeg += abs(applied)
        return applied
    }

    private fun signedDelta(target: Double, from: Double): Double {
        var d = (target - from) % 360.0
        if (d > 180) d -= 360.0
        if (d < -180) d += 360.0
        return d
    }

    // ------------------------------------------------------------------ inputs

    /** Latest attitude, as the rotation-vector values straight off the sensor. */
    fun onRotationVector(values: FloatArray) {
        SensorManager.getRotationMatrixFromVector(rotation, values)
        haveRotation = true
    }

    fun onGyro(x: Float, y: Float, z: Float) {
        lastGyroNorm = sqrt((x * x + y * y + z * z).toDouble())
    }

    /**
     * One accelerometer sample. [tNs] is the sensor's own elapsed-realtime timestamp, so dt comes
     * from the same monotonic clock the CSVs are stamped with.
     */
    fun onAccel(tNs: Long, x: Float, y: Float, z: Float) {
        val prev = lastAccelNs
        lastAccelNs = tNs
        if (prev == 0L || !haveRotation || !initialised) return

        val dt = (tNs - prev) / 1e9
        // A gap means the sensor hub stalled or the FIFO was flushed late. Integrating across it
        // would fabricate a velocity step, so the sample is dropped instead.
        if (dt <= 0.0 || dt > MAX_DT_S) return

        // Device frame → ENU. Row-major 3x3 from getRotationMatrixFromVector.
        val aE = rotation[0] * x + rotation[1] * y + rotation[2] * z
        val aN = rotation[3] * x + rotation[4] * y + rotation[5] * z
        val aU = rotation[6] * x + rotation[7] * y + rotation[8] * z

        val norm = sqrt((x * x + y * y + z * z).toDouble())

        // Stand-still detection drives both the gravity estimate and the zero-velocity update.
        val looksStill =
            abs(norm - gravity) < STILL_ACCEL_TOL && lastGyroNorm < STILL_GYRO_TOL
        stillFor = if (looksStill) stillFor + dt else 0.0
        isStationary = stillFor > STILL_HOLD_S

        // Linear acceleration: strip gravity from the up axis, then the learned bias.
        var lE = aE - bE
        var lN = aN - bN
        var lU = aU - gravity - bU

        if (isStationary) {
            // Everything left over while standing still is error, so feed it back. Learning the
            // gravity magnitude here is what absorbs the accelerometer's scale-factor error —
            // this device reads about 0.9% low, and a fixed 9.80665 would inject that straight
            // into the vertical channel.
            gravity += K_GRAVITY * (norm - gravity)
            bE += K_BIAS * lE
            bN += K_BIAS * lN
            bU += K_BIAS * lU
            // Zero-velocity update: standing still means the velocity really is zero, which stops
            // the first integration from accumulating anything at all.
            vE = 0.0; vN = 0.0; vU = 0.0
            lE = 0.0; lN = 0.0; lU = 0.0
        }

        vE += lE * dt
        vN += lN * dt
        vU += lU * dt

        pE += vE * dt
        pN += vN * dt
        pU += vU * dt

        driftMetres = sqrt((pE - anchorE) * (pE - anchorE) + (pN - anchorN) * (pN - anchorN))
    }

    /**
     * Pull the dead-reckoned state back onto a trusted GNSS fix. Called only while the
     * constellation is healthy; during an outage the integrator is left alone so the divergence
     * is real rather than continuously papered over.
     */
    fun anchorTo(lat: Double, lon: Double, speedMps: Float?, bearingDeg: Float?) {
        if (!initialised) setOrigin(lat, lon)

        pE = (lon - originLon) * mPerDegLon
        pN = (lat - originLat) * mPerDegLat
        anchorE = pE
        anchorN = pN
        driftMetres = 0.0

        // GNSS velocity is far better than anything the accelerometer can integrate, so take it
        // whenever it is offered rather than blending.
        if (speedMps != null && bearingDeg != null) {
            val rad = Math.toRadians(bearingDeg.toDouble())
            vE = speedMps * kotlin.math.sin(rad)
            vN = speedMps * kotlin.math.cos(rad)
        }
        vU = 0.0
    }

    // applyMLCorrection was removed deliberately. It took the model's mu - a displacement in
    // METRES - and passed it where this class expects DEGREES, so every call overshot by a
    // factor of about 11. It also added the same offset to both north and east regardless of
    // heading, which drives the estimate northeast at 45 degrees no matter which way the vehicle
    // points, and it accumulated at 10 Hz.
    //
    // When the model is trained and validated, the correct coupling is a velocity update along
    // the current heading, not a position nudge: something like
    //
    //     fun applyModelSpeed(speedMps: Double) {
    //         val h = atan2(vE, vN)
    //         vE = speedMps * sin(h); vN = speedMps * cos(h)
    //     }
    //
    // which is dimensionally sound and lets the existing integration carry it into position.

    private fun setOrigin(lat: Double, lon: Double) {
        originLat = lat
        originLon = lon
        val phi = Math.toRadians(lat)
        // Local metres-per-degree, good to centimetres over the tens of kilometres a session covers.
        mPerDegLat = 111_132.92 - 559.82 * cos(2 * phi) + 1.175 * cos(4 * phi)
        mPerDegLon = 111_412.84 * cos(phi) - 93.5 * cos(3 * phi)
        pE = 0.0; pN = 0.0; pU = 0.0
        vE = 0.0; vN = 0.0; vU = 0.0
        anchorE = 0.0; anchorN = 0.0
    }

    fun reset() {
        originLat = Double.NaN
        originLon = Double.NaN
        lastAccelNs = 0L
        haveRotation = false
        gravity = G_NOMINAL
        bE = 0.0; bN = 0.0; bU = 0.0
        driftMetres = 0.0
        stillFor = 0.0
        isStationary = false
        headingCorrectionDeg = 0.0
    }
}
