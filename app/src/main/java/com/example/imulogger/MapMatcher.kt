package com.example.imulogger

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.sqrt

/**
 * Online HMM map matching: snaps a drifting dead-reckoned position onto the road network.
 *
 * States are candidate positions on nearby road segments; observations are the dead-reckoned
 * fixes. Viterbi runs incrementally with a beam, so each new fix costs O(beam x candidates)
 * rather than a re-run over the whole history.
 *
 * **Departure from Newson & Krumm (2009), deliberately.** Their transition term is the difference
 * between straight-line distance and *route distance through the road graph*, which needs
 * connectivity. Mapsforge stores ways clipped per tile with no node identity, so there is no graph
 * to route over. Route distance also matters most at the sparse sampling their paper targets
 * (30 s+); here fixes arrive every couple of seconds, where consecutive candidates are almost
 * always on the same or an adjacent segment and the straight-line approximation is tight.
 *
 * What replaces it is a heading term, and that is the point rather than a consolation: the whole
 * reason this class exists is that heading integration is where dead reckoning fails. Scoring a
 * candidate on how well the road's bearing agrees with the vehicle's course is precisely the
 * observation the IMU cannot supply for itself.
 */
class MapMatcher(private val roads: RoadNetwork) {

    companion object {
        /** How far from a fix to look for road candidates. Widened as the estimate drifts. */
        private const val BASE_SEARCH_RADIUS_M = 60.0
        private const val MAX_SEARCH_RADIUS_M = 400.0

        /**
         * Emission spread, metres. Newson & Krumm fit ~4.07 m for GPS; this is far larger because
         * the observation here is a *dead-reckoned* position that may be hundreds of metres out,
         * and too tight a sigma would reject the correct road outright.
         */
        private const val EMISSION_SIGMA_M = 30.0

        /** Spread on the step-length mismatch between consecutive candidates, metres. */
        private const val TRANSITION_SIGMA_M = 25.0

        /** Spread on heading disagreement between course and road bearing, degrees. */
        private const val HEADING_SIGMA_DEG = 35.0

        /** Hypotheses kept between steps. Beyond this the tail never wins. */
        private const val BEAM = 12

        /** Candidates scored per step, nearest first. */
        private const val MAX_CANDIDATES = 24

        /** Below this speed heading is meaningless, so the heading term is switched off. */
        private const val MIN_SPEED_FOR_HEADING = 1.5

        /**
         * Confidence a match needs before its bearing is fed back into the integrator.
         *
         * Feedback is not free: a wrong road that is believed becomes a wrong heading that then
         * makes the next match agree with it. Requiring the winning hypothesis to hold a clear
         * majority of the beam keeps the loop off ambiguous junctions and parallel carriageways.
         */
        const val HEADING_FEEDBACK_MIN_CONFIDENCE = 0.6

        /**
         * How far a candidate's bearing must differ from the winner's before it counts as a
         * genuine alternative direction rather than another piece of the same road.
         */
        private const val RIVAL_BEARING_DEG = 30.0

        /** Share of the heading disagreement absorbed per accepted match. */
        const val HEADING_FEEDBACK_GAIN = 0.35

        /**
         * Smallest correction always allowed, metres. Roughly a lane width plus map error: even a
         * perfectly dead-reckoned position sits off the centreline by about this much.
         */
        private const val MIN_CORRECTION_BUDGET_M = 12.0

        private const val M_PER_DEG_LAT = 111_132.0
    }

    /** One surviving Viterbi path, identified by its head state. */
    private class Hypothesis(
        val lat: Double,
        val lon: Double,
        val segment: RoadNetwork.Segment,
        val logProb: Double,
    )

    class Match(
        val lat: Double,
        val lon: Double,
        /** Metres the fix was moved to reach the road. */
        val correctionM: Double,
        val roadClass: String,
        /** Bearing of the matched road, which is what gets fed back as a heading observation. */
        val roadBearingDeg: Double,
        val confidence: Double,
    )

    private var beam: List<Hypothesis> = emptyList()
    private var lastLat = Double.NaN
    private var lastLon = Double.NaN

    fun reset() {
        beam = emptyList()
        lastLat = Double.NaN
        lastLon = Double.NaN
    }

    /**
     * Advance one observation.
     *
     * [courseDeg] is the direction of travel the integrator believes it is on, and [speedMps] its
     * speed. [uncertaintyM] widens the search as dead reckoning drifts, so a badly drifted fix can
     * still reach the road it is really on.
     *
     * Returns null when no road is near enough to be plausible, which is the honest answer off-road
     * or when the map has no coverage — better than snapping to something 300 m away.
     */
    fun update(
        lat: Double,
        lon: Double,
        courseDeg: Double?,
        speedMps: Double,
        uncertaintyM: Double,
    ): Match? {
        val radius = (BASE_SEARCH_RADIUS_M + uncertaintyM).coerceAtMost(MAX_SEARCH_RADIUS_M)
        val candidates = roads.candidatesNear(lat, lon, radius)
            .sortedBy { it.distanceM }
            .take(MAX_CANDIDATES)

        if (candidates.isEmpty()) {
            // Losing the road breaks the chain; starting clean beats carrying stale hypotheses.
            beam = emptyList()
            lastLat = lat
            lastLon = lon
            return null
        }

        // How far the vehicle actually moved since the last observation, from the integrator.
        val stepM = if (lastLat.isNaN()) 0.0 else metres(lastLat, lastLon, lat, lon)
        val useHeading = courseDeg != null && speedMps >= MIN_SPEED_FOR_HEADING

        val next = ArrayList<Hypothesis>(candidates.size)
        for (c in candidates) {
            // Emission: how well the road explains where we think we are.
            var score = -0.5 * (c.distanceM / EMISSION_SIGMA_M).let { it * it }

            // Heading: a road running across our course cannot be the one we are on. Undirected,
            // because a two-way road is equally valid travelled in either direction.
            if (useHeading) {
                val delta = if (c.segment.oneway) {
                    abs(bearingDelta(courseDeg!!, c.segment.bearingDeg))
                } else {
                    undirectedBearingDelta(courseDeg!!, c.segment.bearingDeg)
                }
                score += -0.5 * (delta / HEADING_SIGMA_DEG).let { it * it }
            }

            // Transition: consistency with where the previous best states could have reached.
            val best = if (beam.isEmpty()) 0.0 else beam.maxOf { h ->
                val hop = metres(h.lat, h.lon, c.lat, c.lon)
                val mismatch = abs(hop - stepM)
                var t = -0.5 * (mismatch / TRANSITION_SIGMA_M).let { it * it }
                // Staying on the same road is the common case; a small bonus stops the matcher
                // flickering between parallel candidates at a junction.
                if (h.segment === c.segment) t += 0.5
                h.logProb + t
            }
            next.add(Hypothesis(c.lat, c.lon, c.segment, score + best))
        }

        // Normalise so log-probabilities do not run away over a long session.
        val top = next.maxOf { it.logProb }
        beam = next.map { Hypothesis(it.lat, it.lon, it.segment, it.logProb - top) }
            .sortedByDescending { it.logProb }
            .take(BEAM)

        lastLat = lat
        lastLon = lon

        val head = beam.first()

        // Confidence is specifically confidence in the *heading*, because that is the only thing
        // it gates. A softmax share over the whole beam measures the wrong quantity: most of the
        // beam is consecutive segments of the same road, so the winner's share stays near 1/beam
        // (measured median 0.19) even when every candidate agrees on direction. What matters is
        // whether a candidate pointing a genuinely different way is competitive — a cross street
        // at a junction — so confidence is the two-way contest against the best such rival.
        val rival = beam.firstOrNull {
            undirectedBearingDelta(it.segment.bearingDeg, head.segment.bearingDeg) > RIVAL_BEARING_DEG
        }
        val confidence = if (rival == null) {
            1.0
        } else {
            val h = exp(head.logProb)
            val r = exp(rival.logProb)
            if (h + r > 0) h / (h + r) else 0.0
        }
        val correction = metres(lat, lon, head.lat, head.lon)

        // Never move the estimate further than we believe it is wrong by.
        //
        // Measured across four recorded sessions: at ~37 m drift — about the local road spacing —
        // snapping cut position error by 30%. But with drift near zero it made things 40x worse,
        // dragging a fix that sat 0.6 m from truth onto a road 25 m away. Being unaided is not the
        // same as being wrong yet, so the gate is the uncertainty itself rather than GNSS state.
        // The floor keeps a small correction available when drift is still near zero, since road
        // centrelines and the true driving line differ by a lane's width regardless.
        val budget = max(uncertaintyM, MIN_CORRECTION_BUDGET_M)
        if (correction > budget) return null

        return Match(
            head.lat,
            head.lon,
            correction,
            head.segment.roadClass,
            head.segment.bearingDeg,
            confidence,
        )
    }

    private fun metres(aLat: Double, aLon: Double, bLat: Double, bLon: Double): Double {
        val mLon = M_PER_DEG_LAT * cos(Math.toRadians((aLat + bLat) / 2))
        val dx = (bLon - aLon) * mLon
        val dy = (bLat - aLat) * M_PER_DEG_LAT
        return sqrt(dx * dx + dy * dy)
    }
}
