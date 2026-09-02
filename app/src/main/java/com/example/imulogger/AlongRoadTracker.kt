package com.example.imulogger

/**
 * Along-road tracking: position as a scalar walked along a road polyline.
 *
 * Once the vehicle is confidently on a known road, the road can supply direction and the
 * integrator only has to supply distance. That removes heading error from the answer entirely,
 * rather than merely nudging it the way [MapMatcher] plus [DeadReckoner.applyHeadingCorrection]
 * does. It is the mechanism behind the accuracy of the server-side matcher this project is
 * benchmarked against, and [RoadGraph] shows it needs no server.
 *
 * Measured on three recorded sessions (46-57 synthetic outages per duration), against the
 * DR + heading-feedback baseline:
 *
 * ```
 *                            10 s    30 s    60 s
 *   baseline                 10.2    36.0    87.5   m CRSE
 *   along-road, DR distance  12.6    40.8   104.7   (+20%, worse)
 *   along-road, true distance 8.3    22.3    50.5   (-42%)
 *   DR distance bias         -8.4   -29.7   -74.0   m
 * ```
 *
 * So the idea works and the implementation works: given a trustworthy distance the road network
 * cuts 60 s outage error nearly in half. What it cannot survive is the distance it is currently
 * fed. Free-running, the integrator accumulates about 37% less distance than was actually
 * travelled, and along-road tracking converts that shortfall directly into along-track error with
 * no cross-track saving left to pay for it.
 *
 * Hence [enabled] defaults to false. This is finished machinery waiting on its input - the
 * displacement head of the ML model, which predicts exactly this quantity - not a feature to
 * switch on and hope. Turning it on before the distance channel is fixed measurably makes the app
 * worse.
 *
 * Wrong-turn selection was ruled out as the cause: penalising branches that disagree with the
 * integrator's course changed nothing at any weight tried (+19.4% to +24.2%).
 */
class AlongRoadTracker {

    companion object {
        /**
         * Master switch. Off until the integrator's distance channel is trustworthy - see the
         * class comment for the measurement, and [DeadReckoner] for where the shortfall comes
         * from. Flip this once displacement is sourced from something better than double
         * integration, and re-run eval/outage_eval.py before believing it.
         */
        @Volatile
        var enabled = false

        /**
         * Displacement since the last GNSS anchor at which the road network takes over, metres.
         *
         * Note this is [DeadReckoner.driftMetres], which measures how far the vehicle has *moved*
         * since the anchor, not how wrong it is. That is the right variable to gate on even
         * though it is not the error itself: error accumulates with distance travelled rather
         * than with elapsed time, so a stationary vehicle correctly never hands over, and a fast
         * one hands over sooner. 60 m is roughly the 20 s of free-running at our recorded pace
         * where the earlier time-based sweep put the crossover.
         */
        const val HANDOVER_DISPLACEMENT_M = 60.0

        /**
         * Anti-noise floor, seconds. Displacement is jumpy in the first seconds after an anchor
         * is lost and one spike past the gate is not evidence the integrator has lost the plot.
         */
        const val MIN_UNAIDED_S = 4.0

        /** How far from the current estimate to look for a road to commit to, metres. */
        private const val CAPTURE_RADIUS_M = 80.0

        /**
         * Give up if the walk cannot be placed on the network. Falling back to dead reckoning is
         * better than reporting a position on a road we have no evidence for.
         */
        private const val MAX_MISSES = 3

        /**
         * Whether to stop trusting the integrator's heading and hand over to the road network.
         *
         * Stateless on purpose, so the logger thread can ask without touching the matcher
         * thread's tracker.
         */
        fun shouldHandOver(unaidedSeconds: Double, displacementM: Double): Boolean =
            enabled && unaidedSeconds >= MIN_UNAIDED_S && displacementM >= HANDOVER_DISPLACEMENT_M
    }

    /** Where the walk has reached. [alternatives] is how many routes remain live. */
    class Fix(
        val lat: Double,
        val lon: Double,
        val headingDeg: Double,
        val roadClass: String,
        val alternatives: Int,
    )

    private var graph: RoadGraph? = null
    private var hypotheses: List<RoadGraph.State> = emptyList()
    private var misses = 0

    val isTracking: Boolean get() = hypotheses.isNotEmpty()

    /** True when [candidate] is the graph this tracker is already walking on. */
    fun isTrackingOn(candidate: RoadGraph): Boolean = isTracking && graph === candidate

    /**
     * Commit to a road at the current estimate. Returns false when nothing drivable is close
     * enough, leaving the caller on dead reckoning.
     */
    fun start(candidate: RoadGraph, lat: Double, lon: Double, courseDeg: Double?): Boolean {
        val state = candidate.nearestState(lat, lon, courseDeg, CAPTURE_RADIUS_M)
        if (state == null) {
            reset()
            return false
        }
        graph = candidate
        hypotheses = listOf(state)
        misses = 0
        return true
    }

    /**
     * Advance every live route by [distanceM] - the distance the integrator says was travelled
     * since the last call - and return the best position.
     *
     * Returns null once the walk has run out of network repeatedly, which is the signal to hand
     * back to dead reckoning rather than keep guessing.
     */
    fun advance(distanceM: Double): Fix? {
        val g = graph ?: return null
        if (hypotheses.isEmpty()) return null

        val next = g.advance(hypotheses, if (distanceM > 0) distanceM else 0.0)
        if (next.isEmpty()) {
            if (++misses >= MAX_MISSES) reset()
            return null
        }
        misses = 0
        hypotheses = next

        val best = next.first()
        val p = g.pointAt(best.segment, best.offsetM, best.direction)
        return Fix(
            p.lat, p.lon,
            g.headingOf(best.segment, best.direction),
            g.segments[best.segment].roadClass,
            next.size,
        )
    }

    fun reset() {
        graph = null
        hypotheses = emptyList()
        misses = 0
    }
}
