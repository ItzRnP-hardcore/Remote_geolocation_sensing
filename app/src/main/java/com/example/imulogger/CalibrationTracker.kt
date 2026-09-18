package com.example.imulogger

import android.hardware.SensorManager
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.sqrt

/**
 * Decides when the figure-eight compass gesture has been done well enough.
 *
 * The previous dialog asked the user "is the magnetometer heading accurate?", which nobody
 * standing in a car park can answer, and defaulted them toward the model yaw head - the option
 * that measured ~100% drift. This replaces the question with a measurement.
 *
 * The magnetometer's hard-iron offset can only be fitted from readings taken with the phone
 * pointing many different ways; that is the entire purpose of the gesture. So progress is the
 * share of directions the field has been seen from, in the phone's own axes, on a sphere cut
 * into [CELLS] equal-area patches. Equal-area matters: latitude/longitude cells crowd at the
 * poles and a phone waved gently about one axis would score as though it had covered them.
 *
 * Android's own accuracy flag is combined with it, because the OS runs its own fit and knows
 * things this class does not. Either route to "done" is accepted:
 *
 *  - the OS reports HIGH accuracy and the gesture covered at least [MIN_COVERAGE_WITH_HIGH], or
 *  - the gesture covered [TARGET_COVERAGE] and the OS reports at least MEDIUM.
 *
 * The coverage floors keep a phone that merely *arrived* calibrated from skipping the gesture
 * entirely: accuracy survives from earlier use, the vehicle's interference does not.
 */
class CalibrationTracker {

    companion object {
        private const val BANDS = 6          // equal-area bands in z = sin(elevation)
        private const val SECTORS = 12       // azimuth sectors per band
        const val CELLS = BANDS * SECTORS

        const val TARGET_COVERAGE = 0.40
        const val MIN_COVERAGE_WITH_HIGH = 0.25

        /** A flick of the wrist is not a calibration, however lucky its coverage. */
        const val MIN_DURATION_S = 4.0
    }

    private val visited = BooleanArray(CELLS)
    private var visitedCount = 0
    private var firstNs = 0L
    private var lastNs = 0L

    /** Latest SensorManager accuracy for the magnetometer. */
    var accuracy: Int = SensorManager.SENSOR_STATUS_UNRELIABLE
        private set

    val coverage: Double get() = visitedCount.toDouble() / CELLS

    val durationS: Double get() = if (firstNs == 0L) 0.0 else (lastNs - firstNs) / 1e9

    /** Progress toward done, 0..1, for the animation to fill. */
    val progress: Double
        get() {
            val target = if (accuracy >= SensorManager.SENSOR_STATUS_ACCURACY_HIGH)
                MIN_COVERAGE_WITH_HIGH else TARGET_COVERAGE
            val byCoverage = (coverage / target).coerceIn(0.0, 1.0)
            val byTime = (durationS / MIN_DURATION_S).coerceIn(0.0, 1.0)
            return minOf(byCoverage, byTime)
        }

    val done: Boolean
        get() = durationS >= MIN_DURATION_S && (
            (accuracy >= SensorManager.SENSOR_STATUS_ACCURACY_HIGH &&
                coverage >= MIN_COVERAGE_WITH_HIGH) ||
                (accuracy >= SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM &&
                    coverage >= TARGET_COVERAGE))

    fun reset() {
        visited.fill(false)
        visitedCount = 0
        firstNs = 0L
        lastNs = 0L
        accuracy = SensorManager.SENSOR_STATUS_UNRELIABLE
    }

    /**
     * One magnetometer event. [accuracy] is taken from the EVENT as well as from
     * onAccuracyChanged: the latter fires only on a change, so a phone that is already
     * calibrated when the dialog opens would otherwise read "uncalibrated" forever.
     */
    fun onSample(tNs: Long, x: Float, y: Float, z: Float, accuracy: Int) {
        this.accuracy = accuracy
        if (firstNs == 0L) firstNs = tNs
        lastNs = tNs
        val n = sqrt((x * x + y * y + z * z).toDouble())
        if (n < 1.0) return                       // below any real field: sensor glitch
        val cell = cellOf(x / n, y / n, z / n)
        if (!visited[cell]) {
            visited[cell] = true
            visitedCount++
        }
    }

    fun onAccuracy(accuracy: Int) {
        this.accuracy = accuracy
    }

    /** Equal-area cell index for a unit vector. */
    internal fun cellOf(ux: Double, uy: Double, uz: Double): Int {
        // Uniform in z is uniform in area on a sphere (Archimedes' hat-box theorem).
        val band = floor((uz.coerceIn(-1.0, 1.0) + 1.0) / 2.0 * BANDS).toInt()
            .coerceIn(0, BANDS - 1)
        var a = atan2(uy, ux)
        if (a < 0) a += 2 * PI
        val sector = floor(a / (2 * PI) * SECTORS).toInt().coerceIn(0, SECTORS - 1)
        return band * SECTORS + sector
    }
}
