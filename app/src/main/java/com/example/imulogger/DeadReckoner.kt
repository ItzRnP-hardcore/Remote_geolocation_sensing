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

        /**
         * How fast the DEVICE-frame gyroscope bias is learned during stand-still.
         *
         * Slower than [K_BIAS] because the quantity is smaller and matters more. Measured across
         * the IO-VNBD runs the per-session gyro offset is only 1.3-2.9% of a channel's spread,
         * but heading is its integral: 0.004 rad/s is 825 deg/hr, and swapping a model yaw head
         * for this debiased channel took 300 s free-running drift from 51.8% to 19.3%. A noisy
         * estimate of a small number would give that back, so it is averaged hard.
         */
        const val K_GYRO_BIAS = 0.005

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

        /**
         * Below this the velocity vector has no direction to preserve, so a speed estimate
         * cannot be applied: scaling a zero vector leaves it zero, and inventing a direction
         * would be worse than declining. A model that believes the vehicle is moving while the
         * integrator believes it is stopped is a disagreement only GNSS can settle.
         */
        const val MIN_SPEED_FOR_MODEL_FIX = 0.3
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

    /**
     * Device-frame gyroscope bias, learned during stand-still.
     *
     * Device frame, not world: the offset is a property of the sensor die, so it is fixed in the
     * handset's own axes and rotating it into ENU first would smear one constant across three
     * channels as the phone turns.
     */
    private var gbX = 0.0
    private var gbY = 0.0
    private var gbZ = 0.0

    /** Whether stand-still has been seen for long enough to trust [gyroBias]. */
    var gyroBiasValid: Boolean = false
        private set

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

    /** Total m/s the model has adjusted speed by this session, for observability. */
    var modelSpeedCorrectionMps: Double = 0.0
        private set

    /**
     * Rotate the course toward a road bearing, keeping speed untouched.
     *
     * This is the whole point of map matching for this project: a road's bearing is an observation
     * of exactly the quantity the IMU cannot recover for itself.
     *
     * Speed is left alone here because the map has nothing to say about it, NOT because it is
     * good. Session-average speed looks competitive (3.10 m/s against GPS's 3.36 on a recorded
     * run), but that average is flattered by the anchored stretches. Measured over free-running
     * outages alone, this integrator accumulates about 37% less distance than was actually
     * travelled - 74 m short over 60 s - which is now the single largest error source in the
     * system and the reason [AlongRoadTracker] is disabled by default.
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
        if (isStationary) {
            // Standing still means the true rate is zero, so whatever the gyro reports is offset.
            // [isStationary] is maintained on the accelerometer tick; both streams arrive at
            // 200 Hz, so it is at most one sample stale here, which cannot matter for a term
            // averaged with a gain of 0.005.
            gbX += K_GYRO_BIAS * (x - gbX)
            gbY += K_GYRO_BIAS * (y - gbY)
            gbZ += K_GYRO_BIAS * (z - gbZ)
            gyroBiasValid = true
        }
    }

    /** Learned gyroscope bias in device axes, rad/s. Zero until a stop has been observed. */
    val gyroBias: DoubleArray get() = doubleArrayOf(gbX, gbY, gbZ)

    /**
     * Copy the current device-to-world rotation out, row major. False before the first sample.
     *
     * Shared so the model's levelling uses the same attitude the integrator navigates on.
     * Measured on session 20260904_195146 against GNSS bearing, the rotation vector tracks the
     * road to 10.8 deg RMS while `getRotationMatrix(gravity, magnetometer)` manages 12.6 - and
     * the magnetometer is the one sensor a steel vehicle actively disturbs.
     */
    fun rotationInto(out: FloatArray): Boolean {
        if (!haveRotation) return false
        System.arraycopy(rotation, 0, out, 0, 9)
        return true
    }

    /**
     * Remove the learned offset from one gyroscope sample, in place, in device axes.
     *
     * Exposed rather than applied internally because the integrator takes its attitude from the
     * rotation vector and never integrates this channel itself. The consumer is the model: it is
     * trained on `--debias all` features, so feeding it the raw channel is a train/serve skew.
     */
    fun debiasGyro(out: FloatArray, x: Float, y: Float, z: Float) {
        out[0] = (x - gbX).toFloat()
        out[1] = (y - gbY).toFloat()
        out[2] = (z - gbZ).toFloat()
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

    /**
     * Blend a model speed estimate into the velocity magnitude, leaving direction untouched.
     *
     * This is the coupling the deleted `applyMLCorrection` should have been. That one took the
     * model's mu - a speed in m/s - and passed it where this class expected degrees, then added
     * the same offset to north and east regardless of heading, driving the estimate northeast at
     * 45 degrees whatever way the vehicle pointed, at 10 Hz. Speed is a scalar observation of the
     * velocity vector's magnitude and nothing else, so that is all it is allowed to touch here;
     * the existing integration carries it into position on its own.
     *
     * [weight] is a scalar Kalman gain in [0, 1] formed from the model's reported variance against
     * an assumed integrator variance - see [IMUModelRunner.fusionWeight]. A model that declares
     * itself uncertain therefore moves the estimate less, which is the entire reason the network
     * has a logvar head at all.
     *
     * Returns the m/s actually applied, signed.
     */
    fun applyModelSpeed(modelSpeedMps: Double, weight: Double): Double {
        val sp = speed
        if (sp < MIN_SPEED_FOR_MODEL_FIX) return 0.0
        val w = weight.coerceIn(0.0, 1.0)
        if (w <= 0.0) return 0.0

        val target = modelSpeedMps.coerceAtLeast(0.0)
        val blended = sp + w * (target - sp)
        val delta = blended - sp
        val scale = blended / sp
        vE *= scale
        vN *= scale
        modelSpeedCorrectionMps += abs(delta)
        return delta
    }

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
        gbX = 0.0; gbY = 0.0; gbZ = 0.0
        gyroBiasValid = false
        headingCorrectionDeg = 0.0
        modelSpeedCorrectionMps = 0.0
    }
}
