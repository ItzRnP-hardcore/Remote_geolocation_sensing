package com.example.imulogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

class CompassHeadingTest {

    /** Row-major device-to-ENU rotation for a phone lying flat, +Y pointing at [azDeg]. */
    private fun flatPhone(azDeg: Double): FloatArray {
        val a = Math.toRadians(azDeg)
        // Columns are device X, Y, Z in ENU. Y at azimuth a; X 90 deg clockwise of it; Z up.
        return floatArrayOf(
            cos(a).toFloat(), sin(a).toFloat(), 0f,
            (-sin(a)).toFloat(), cos(a).toFloat(), 0f,
            0f, 0f, 1f,
        )
    }

    private fun angleDiff(a: Double, b: Double) = abs(CompassHeading.wrap180(a - b))

    @Test
    fun learnsMountOffsetFromStraightDriving() {
        val c = CompassHeading()
        c.onRotation(flatPhone(100.0), 0f, 0f, 9.81f)
        assertEquals(100.0, c.phoneAzimuthDeg, 1e-6)
        assertFalse(c.mountCalibrated)
        repeat(20) { i -> c.onGnss(118.0 + (if (i % 2 == 0) 1.0 else -1.0), 12.0, 0.0) }
        assertTrue(c.mountCalibrated)
        assertEquals(18.0, c.mountOffsetDeg, 0.5)
        assertEquals(118.0, c.compassVehicleDeg, 0.5)
    }

    @Test
    fun offsetWrapsThroughNorthInsteadOfAveragingToSouth() {
        val c = CompassHeading()
        c.onRotation(flatPhone(350.0), 0f, 0f, 9.81f)
        repeat(20) { c.onGnss(10.0, 12.0, 0.0) }
        // +20 deg, not -340: a linear mean of 350 and 10 would say 180.
        assertEquals(20.0, c.mountOffsetDeg, 0.5)
        assertEquals(0.0, angleDiff(c.compassVehicleDeg, 10.0), 0.5)
    }

    @Test
    fun ignoresTurningAndSlowFixesWhenLearningTheMount() {
        val c = CompassHeading()
        c.onRotation(flatPhone(0.0), 0f, 0f, 9.81f)
        repeat(30) { c.onGnss(90.0, 2.0, 0.0) }      // too slow
        repeat(30) { c.onGnss(90.0, 12.0, 15.0) }    // turning
        assertEquals(0, c.mountSamples)
        assertFalse(c.mountCalibrated)
    }

    @Test
    fun reseatingOnTheStandRelearnsTheOffset() {
        val c = CompassHeading()
        c.onRotation(flatPhone(0.0), 0f, 0f, 9.81f)
        repeat(20) { c.onGnss(30.0, 12.0, 0.0) }
        assertEquals(30.0, c.mountOffsetDeg, 0.5)
        // Stood upright on a dash stand: gravity now along device Y, so -Z becomes forward.
        c.onRotation(flatPhone(0.0), 0f, 9.81f, 0f)
        assertEquals(0, c.mountSamples)
        assertFalse(c.mountCalibrated)
    }

    @Test
    fun filterFollowsTheGyroAndIsPulledBackByTheCompass() {
        val c = CompassHeading()
        c.onRotation(flatPhone(0.0), 0f, 0f, 9.81f)
        c.onMagnetometer(0f, 30f, -30f)
        repeat(20) { c.onGnss(0.0, 12.0, 0.0) }       // offset 0, seeded at 0
        // A biased gyro (+2 deg/s) for 60 s would reach 120 deg on its own.
        var h = 0.0
        repeat(600) { h = c.step(Math.toRadians(2.0), 0.1) }
        assertTrue("compass must bound gyro drift, got $h", angleDiff(h, 0.0) < 10.0)
    }

    @Test
    fun disturbedFieldFallsBackToGyroOnly() {
        val c = CompassHeading()
        c.onRotation(flatPhone(0.0), 0f, 0f, 9.81f)
        c.onMagnetometer(0f, 30f, -30f)                // ~42.4 uT reference
        repeat(20) { c.onGnss(0.0, 12.0, 0.0) }
        c.onMagnetometer(0f, 60f, -60f)                // doubled: a steel structure
        assertTrue(c.disturbed)
        var h = 0.0
        repeat(100) { h = c.step(Math.toRadians(1.0), 0.1) }
        // Pure integration: 10 s at 1 deg/s. The compass (still at 0) must be ignored.
        assertEquals(10.0, h, 0.2)
    }

    @Test
    fun noHeadingBeforeTheMountIsKnown() {
        val c = CompassHeading()
        c.onRotation(flatPhone(0.0), 0f, 0f, 9.81f)
        assertTrue(c.step(0.0, 0.1).isNaN())
    }
}

class CalibrationTrackerTest {

    private val HIGH = 3
    private val MEDIUM = 2
    private val LOW = 1

    @Test
    fun equalAreaCellsAreAllReachable() {
        val t = CalibrationTracker()
        val seen = HashSet<Int>()
        // Fibonacci sphere: evenly spread directions must land in every cell.
        val n = 4000
        val golden = Math.PI * (3 - Math.sqrt(5.0))
        for (i in 0 until n) {
            val z = 1 - 2.0 * (i + 0.5) / n
            val r = Math.sqrt(1 - z * z)
            val th = golden * i
            seen.add(t.cellOf(r * cos(th), r * sin(th), z))
        }
        assertEquals(CalibrationTracker.CELLS, seen.size)
    }

    @Test
    fun aStillPhoneNeverCompletes() {
        val t = CalibrationTracker()
        for (i in 0 until 400) t.onSample(i * 20_000_000L, 0f, 30f, -30f, HIGH)
        assertTrue(t.coverage < 0.05)
        assertFalse(t.done)
    }

    @Test
    fun aQuickFlickDoesNotCountEvenIfLucky() {
        val t = CalibrationTracker()
        sweep(t, seconds = 1.0, accuracy = HIGH)
        assertFalse("under the minimum duration", t.done)
    }

    @Test
    fun aThoroughGestureCompletes() {
        val t = CalibrationTracker()
        sweep(t, seconds = 6.0, accuracy = MEDIUM)
        assertTrue("coverage ${t.coverage}", t.coverage >= CalibrationTracker.TARGET_COVERAGE)
        assertTrue(t.done)
        assertEquals(1.0, t.progress, 1e-9)
    }

    @Test
    fun lowAccuracyBlocksCompletionWhateverTheCoverage() {
        val t = CalibrationTracker()
        sweep(t, seconds = 6.0, accuracy = LOW)
        assertFalse(t.done)
    }

    /** A tumbling figure-eight: the field swept through many device-frame directions. */
    private fun sweep(t: CalibrationTracker, seconds: Double, accuracy: Int) {
        val steps = (seconds * 50).toInt()
        for (i in 0 until steps) {
            val s = i / 50.0
            val a = 2 * Math.PI * s / 2.4
            val x = cos(a) * cos(2 * a)
            val y = sin(a) * cos(2 * a)
            val z = sin(2 * a + 0.7 * s)
            t.onSample((s * 1e9).toLong(), (40 * x).toFloat(), (40 * y).toFloat(),
                (40 * z).toFloat(), accuracy)
        }
    }
}
