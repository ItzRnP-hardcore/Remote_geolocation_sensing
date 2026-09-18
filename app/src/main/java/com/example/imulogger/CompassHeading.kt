package com.example.imulogger

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * The car's heading from the phone's compass, for use when GNSS is gone.
 *
 * Why this exists. Measured on session 20260904_195146, with GNSS truth speed so that only
 * heading differs, over simulated outages:
 *
 * ```
 *   heading source                     30 s drift   60 s drift   heading RMS
 *   model yaw head (previous default)    105.5%       96.6%        103.5 deg
 *   game_rv azimuth                       93.0%       96.2%         64.1 deg
 *   gyro, debiased, integrated            27.1%       21.7%         18.9 deg
 *   compass, no mount offset              31.5%       29.0%         19.7 deg
 *   compass + mount offset                 8.7%        7.5%          7.2 deg
 *   compass + gyro, this class            ~11%        ~6%           ~9 deg
 * ```
 *
 * One drive, 188 s - enough to rank the sources decisively, not enough to quote a final number.
 *
 * Two calibrations, and both are needed:
 *
 *  1. **The phone**: the figure-eight gesture lets Android fit the magnetometer's hard- and
 *     soft-iron offsets. That happens in the OS; [CalibrationTracker] only tells the user when
 *     it has been done well enough.
 *  2. **The mount**: on a stand the phone does not point where the car points. A phone
 *     rotated 18 deg on its cradle is a constant 18 deg heading error, and at that angle the
 *     track leaves the road by a third of the distance driven. The offset is learned here from
 *     GNSS, on straight brisk driving, while GNSS is still available - which is exactly when
 *     it costs nothing.
 *
 * Output is a complementary filter: the debiased gyro carries heading between compass looks
 * and the compass pulls it back with time constant [TAU_S]. The compass is ignored while the
 * field magnitude is off its reference by more than [DISTURBED_FRACTION] - steel bridges,
 * tunnel reinforcement and passing lorries all do that - so a disturbance degrades the
 * estimate to gyro-only rather than dragging it off.
 *
 * Headings are degrees clockwise from true north throughout. Not thread-safe; confined to the
 * logger thread like [DeadReckoner].
 */
class CompassHeading {

    companion object {
        /** GNSS bearing is only a trustworthy heading while moving briskly... */
        const val CAL_MIN_SPEED_MPS = 4.0

        /** ...in a straight line, where the car's heading equals its course over ground. */
        const val CAL_MAX_TURN_RATE_DPS = 3.0

        /** GNSS samples needed before the mount offset is trusted. ~10 s of straight road. */
        const val CAL_MIN_SAMPLES = 10

        /** Decay per straight GNSS fix for the mount-offset estimate; ~50 fixes of memory. */
        const val MOUNT_MEMORY = 0.98

        /**
         * Complementary-filter time constant, seconds. Swept on the recorded drive: 2 to 8 s
         * are within noise of each other at 60 s (5.6-6.6%); 16 s is clearly worse. Chosen in
         * the flat part rather than at its measured minimum, because one drive cannot tell
         * which end of a flat region generalises.
         */
        const val TAU_S = 3.0

        /** Field-magnitude deviation beyond which the compass is not believed. */
        const val DISTURBED_FRACTION = 0.15

        /**
         * Device axis used as the phone's "forward". +Y (top edge) suits a phone lying flat;
         * -Z (camera side) suits one standing on a dash stand, where getOrientation's azimuth
         * becomes ill-conditioned. The flatter of the two is chosen, and the mount offset
         * absorbs whichever constant angle separates it from the car.
         */
        private val AXIS_Y = doubleArrayOf(0.0, 1.0, 0.0)
        private val AXIS_MINUS_Z = doubleArrayOf(0.0, 0.0, -1.0)

        fun wrap180(deg: Double): Double {
            var d = (deg + 180.0) % 360.0
            if (d < 0) d += 360.0
            return d - 180.0
        }

        fun wrap360(deg: Double): Double {
            var d = deg % 360.0
            if (d < 0) d += 360.0
            return d
        }
    }

    /** Which device axis is currently "forward"; chosen from gravity, see [AXIS_MINUS_Z]. */
    private var forwardAxis = AXIS_Y

    /** Latest compass azimuth of [forwardAxis], degrees. NaN before the first rotation. */
    var phoneAzimuthDeg: Double = Double.NaN
        private set

    // Circular accumulation of (GNSS bearing - phone azimuth).
    private var sumSin = 0.0
    private var sumCos = 0.0
    var mountSamples = 0
        private set

    /** Learned phone-to-car offset, degrees. Meaningful once [mountCalibrated]. */
    var mountOffsetDeg: Double = 0.0
        private set

    val mountCalibrated: Boolean get() = mountSamples >= CAL_MIN_SAMPLES

    /** Field magnitude captured while the mount was learned, uT. NaN until then. */
    private var fieldRefUt = Double.NaN
    private var fieldSum = 0.0
    private var fieldN = 0

    private var lastFieldUt = Double.NaN

    /** True while the magnetometer disagrees with its own reference magnitude. */
    val disturbed: Boolean
        get() = !fieldRefUt.isNaN() && !lastFieldUt.isNaN() &&
            abs(lastFieldUt / fieldRefUt - 1.0) > DISTURBED_FRACTION

    /** Filtered vehicle heading, degrees clockwise from north. NaN until seeded. */
    var headingDeg: Double = Double.NaN
        private set

    /** Compass reading for the car itself: phone azimuth plus mount offset. NaN if unknown. */
    val compassVehicleDeg: Double
        get() = if (mountCalibrated && !phoneAzimuthDeg.isNaN())
            wrap360(phoneAzimuthDeg + mountOffsetDeg) else Double.NaN

    fun reset() {
        forwardAxis = AXIS_Y
        phoneAzimuthDeg = Double.NaN
        sumSin = 0.0; sumCos = 0.0; mountSamples = 0; mountOffsetDeg = 0.0
        fieldRefUt = Double.NaN; fieldSum = 0.0; fieldN = 0; lastFieldUt = Double.NaN
        headingDeg = Double.NaN
    }

    /** Latest magnetometer sample, device axes, uT. */
    fun onMagnetometer(x: Float, y: Float, z: Float) {
        lastFieldUt = sqrt((x * x + y * y + z * z).toDouble())
    }

    /**
     * Latest attitude: a row-major device-to-ENU rotation, and gravity in device axes.
     *
     * The rotation MUST be magnetometer-referenced (TYPE_ROTATION_VECTOR). game_rv's yaw has
     * no north; on the recorded drive it sat -19.9 +/- 38.2 deg from the magnetometer's and a
     * different constant again on another day, so an offset learned against it is worthless.
     */
    fun onRotation(r: FloatArray, gravX: Float, gravY: Float, gravZ: Float) {
        val gn = sqrt((gravX * gravX + gravY * gravY + gravZ * gravZ).toDouble())
        if (gn > 1e-3) {
            // Up is -gravity in device axes; how vertical each candidate is, is |axis . up|.
            val tiltY = abs(gravY / gn)
            val tiltZ = abs(gravZ / gn)
            // Hysteresis, so a phone near 45 deg does not flip axes (and offsets) repeatedly.
            val next = when {
                forwardAxis === AXIS_Y && tiltY > tiltZ + 0.2 -> AXIS_MINUS_Z
                forwardAxis === AXIS_MINUS_Z && tiltZ > tiltY + 0.2 -> AXIS_Y
                else -> forwardAxis
            }
            if (next !== forwardAxis) {
                // A different reference axis means a different constant to the car, so the
                // offset learned so far is for the wrong axis. Relearn rather than blend.
                forwardAxis = next
                sumSin = 0.0; sumCos = 0.0; mountSamples = 0
            }
        }
        val a = forwardAxis
        val e = r[0] * a[0] + r[1] * a[1] + r[2] * a[2]
        val n = r[3] * a[0] + r[4] * a[1] + r[5] * a[2]
        if (e * e + n * n < 1e-6) return        // axis vertical: no azimuth to read
        phoneAzimuthDeg = wrap360(Math.toDegrees(atan2(e, n)))
    }

    /**
     * One GNSS fix. Learns the mount offset on straight brisk driving, and re-seeds the
     * filtered heading so an outage always starts from the best available estimate.
     *
     * [turnRateDps] is how fast the GNSS bearing is changing; the caller differences
     * consecutive fixes.
     */
    fun onGnss(bearingDeg: Double, speedMps: Double, turnRateDps: Double) {
        if (speedMps >= CAL_MIN_SPEED_MPS) headingDeg = wrap360(bearingDeg)
        if (phoneAzimuthDeg.isNaN()) return
        if (speedMps < CAL_MIN_SPEED_MPS || abs(turnRateDps) > CAL_MAX_TURN_RATE_DPS) return

        val d = Math.toRadians(wrap180(bearingDeg - phoneAzimuthDeg))
        // Exponentially weighted circular mean rather than a running one: if the phone is
        // re-seated on its stand mid-drive, a cumulative mean would take as long to forget the
        // old offset as it took to learn it. ~50 straight fixes of memory.
        sumSin = sumSin * MOUNT_MEMORY + sin(d)
        sumCos = sumCos * MOUNT_MEMORY + cos(d)
        mountSamples++
        mountOffsetDeg = Math.toDegrees(atan2(sumSin, sumCos))
        if (!lastFieldUt.isNaN()) {
            fieldSum += lastFieldUt
            fieldN++
            fieldRefUt = fieldSum / fieldN
        }
    }

    /**
     * Advance the filter by one gyro step while unaided.
     *
     * [yawRateCwRadS] is the debiased world-vertical rate, clockwise-positive - the same
     * quantity SensorService already derives for [DeadReckoner.onYawRate]. Returns the new
     * heading, or NaN if there is nothing to steer from yet.
     */
    fun step(yawRateCwRadS: Double, dt: Double): Double {
        if (headingDeg.isNaN()) {
            val c = compassVehicleDeg
            if (c.isNaN()) return Double.NaN
            headingDeg = c
        }
        var h = headingDeg + Math.toDegrees(yawRateCwRadS) * dt
        val c = compassVehicleDeg
        if (!c.isNaN() && !disturbed) {
            val k = dt / (TAU_S + dt)
            h += k * wrap180(c - h)
        }
        headingDeg = wrap360(h)
        return headingDeg
    }
}
