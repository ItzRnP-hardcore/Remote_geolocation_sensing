package com.example.imulogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI

class NavigationAndKinematicsTest {

    @Test
    fun testHaversineAccuracy() {
        // Distance between Mumbai (19.0760, 72.8777) and Pune (18.5204, 73.8567) is roughly 120 km
        val distM = NavigationRouter.haversine(19.0760, 72.8777, 18.5204, 73.8567)
        assertTrue("Distance should be roughly 120km", distM > 115000 && distM < 125000)
    }

    @Test
    fun testDeadReckonerNonHolonomicKinematics() {
        val dr = DeadReckoner().apply {
            isPhoneFixed = true
            // Anchor to test coordinates with initial speed 5 m/s and heading East (90 degrees)
            anchorTo(12.9716, 77.5946, 5.0f, 90f)
        }

        val p0 = dr.position
        assertNotNull("Position should be initialized", p0)
        assertEquals("Speed should match anchor speed", 5.0, dr.speed, 0.001)
        assertEquals("Course should be East (90 deg)", 90.0, dr.courseDeg ?: 0.0, 0.5)

        // Steer heading right by +45 deg via onYawRate over 1 second
        dr.onYawRate(Math.toRadians(45.0), 1.0)
        assertEquals("Course should now be 135 deg (SE)", 135.0, dr.courseDeg ?: 0.0, 0.5)

        // Apply model speed correction (10 m/s with weight 0.5 -> 7.5 m/s)
        dr.applyModelSpeed(10.0, 0.5)
        assertEquals("Speed should blend to 7.5 m/s", 7.5, dr.speed, 0.01)

        // Check heading correction towards road bearing 140 deg
        val applied = dr.applyHeadingCorrection(140.0, 0.8)
        assertTrue("Heading correction should nudge toward road", applied > 0.0)
    }

    @Test
    fun testRoadGraphPathfinding() {
        // Create synthetic road segments forming an L-junction:
        // A (10.0, 10.0) -> B (10.001, 10.0) -> C (10.001, 10.001)
        val seg1 = RoadNetwork.Segment(10.0, 10.0, 10.001, 10.0, 0.0, "primary", false)
        val seg2 = RoadNetwork.Segment(10.001, 10.0, 10.001, 10.001, 90.0, "primary", false)

        val graph = RoadGraph(listOf(seg1, seg2))

        val path = graph.findPath(10.0, 10.0, 10.001, 10.001, snapRadiusM = 100.0)
        assertNotNull("Path should be found by A*", path)
        assertTrue("Path should contain points", path!!.points.size >= 2)
        assertTrue("Path distance should be greater than 0", path.distanceM > 0)
    }
}
