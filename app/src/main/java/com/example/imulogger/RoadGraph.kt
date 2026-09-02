package com.example.imulogger

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.roundToLong
import kotlin.math.sqrt

/**
 * Road topology, recovered from Mapsforge geometry alone.
 *
 * [RoadNetwork] can read where the roads are but not how they join up: the format stores ways
 * clipped per tile with no node identity, which is why [MapMatcher] was written to avoid needing
 * connectivity at all. That avoidance costs accuracy. The alternative used elsewhere in this
 * project is to route against an OSRM server, which is exactly what an offline, on-device app
 * cannot do.
 *
 * The way out is that Mapsforge stores coordinates in microdegrees - about 0.11 m - so two
 * segments that shared an OSM node still come back on *identical* coordinates. Snapping endpoints
 * onto a half-metre grid therefore rebuilds the graph without needing node IDs. Measured on 25,383
 * segments around IIT Kharagpur: every segment gained at least one link, 98.5% landed in a single
 * connected component, and mean node degree was 2.73. Tile-boundary clipping heals for free, since
 * both halves of a clipped way end on the same border coordinate.
 *
 * Not thread-safe. Built and used on the matcher thread.
 */
class RoadGraph(val segments: List<RoadNetwork.Segment>) {

    companion object {
        private const val M_PER_DEG_LAT = 111_132.0

        /**
         * Grid cell for merging endpoints. Coincident nodes are bit-identical after Mapsforge's
         * microdegree quantisation, so this only has to be larger than that quantisation and
         * smaller than the shortest real gap between distinct junctions.
         */
        private const val SNAP_TOL_M = 0.5

        /** Route hypotheses carried through junctions. A fork is only resolvable in hindsight. */
        const val BEAM = 8

        /**
         * Cost per degree of turn taken at a junction. Vehicles overwhelmingly go straight on, so
         * a hard turn should need evidence rather than being free; without this the beam happily
         * takes any side road that happens to shorten the walk.
         */
        const val TURN_PENALTY_PER_DEG = 0.02

        /** Junctions one step may cross. Bounds the walk if the map contains zero-length ways. */
        private const val MAX_HOPS = 64
    }

    /** A segment being travelled, and which way along it. [direction] is +1 a-to-b, -1 b-to-a. */
    class State(val segment: Int, val offsetM: Double, val direction: Int, val cost: Double)

    /** A continuation past the far end of the current segment. */
    class Successor(val segment: Int, val direction: Int, val turnDeg: Double)

    val lengthsM = DoubleArray(segments.size)
    private val startNode = IntArray(segments.size)
    private val endNode = IntArray(segments.size)

    /** node -> segment ends touching it, packed as `segment shl 1 or end` (end 0 = a, 1 = b). */
    private val atNode = HashMap<Int, MutableList<Int>>()

    val nodeCount: Int

    init {
        val q = SNAP_TOL_M / M_PER_DEG_LAT
        val ids = HashMap<Long, Int>()

        fun nodeId(lat: Double, lon: Double): Int {
            val key = (((lat / q).roundToLong() and 0xffffffffL) shl 32) or
                ((lon / q).roundToLong() and 0xffffffffL)
            return ids.getOrPut(key) { ids.size }
        }

        for (i in segments.indices) {
            val s = segments[i]
            lengthsM[i] = metres(s.aLat, s.aLon, s.bLat, s.bLon)
            val a = nodeId(s.aLat, s.aLon)
            val b = nodeId(s.bLat, s.bLon)
            startNode[i] = a
            endNode[i] = b
            atNode.getOrPut(a) { ArrayList(2) }.add(i shl 1)
            atNode.getOrPut(b) { ArrayList(2) }.add((i shl 1) or 1)
        }
        nodeCount = ids.size
    }

    /** Bearing travelled when moving along [segment] in [direction]. */
    fun headingOf(segment: Int, direction: Int): Double {
        val b = segments[segment].bearingDeg
        return if (direction > 0) b else (b + 180.0) % 360.0
    }

    /** Coordinates [offsetM] along [segment], measured from whichever end [direction] starts at. */
    fun pointAt(segment: Int, offsetM: Double, direction: Int): TrackPoint {
        val s = segments[segment]
        val len = if (lengthsM[segment] > 1e-9) lengthsM[segment] else 1e-9
        var t = (offsetM / len).coerceIn(0.0, 1.0)
        if (direction < 0) t = 1.0 - t
        return TrackPoint(s.aLat + (s.bLat - s.aLat) * t, s.aLon + (s.bLon - s.aLon) * t)
    }

    /** Segments leaving the far end of [segment], with the turn each one requires. */
    fun successors(segment: Int, direction: Int): List<Successor> {
        val exit = if (direction > 0) endNode[segment] else startNode[segment]
        val touching = atNode[exit] ?: return emptyList()
        val incoming = headingOf(segment, direction)
        val out = ArrayList<Successor>(touching.size)
        for (packed in touching) {
            val j = packed shr 1
            if (j == segment) continue
            // Leaving a shared node means travelling away from it, so a segment touching by its
            // `a` end is entered a-to-b, and one touching by its `b` end is entered b-to-a.
            val d = if ((packed and 1) == 0) 1 else -1
            out.add(Successor(j, d, abs(bearingDelta(headingOf(j, d), incoming))))
        }
        return out
    }

    /**
     * Best place to put a vehicle that believes it is at [lat]/[lon] heading [courseDeg].
     *
     * Returns null when nothing drivable is within [radiusM], which is the honest answer off the
     * network and better than committing to a road far away.
     */
    fun nearestState(lat: Double, lon: Double, courseDeg: Double?, radiusM: Double): State? {
        val mLon = M_PER_DEG_LAT * cos(Math.toRadians(lat))
        var best: State? = null
        for (i in segments.indices) {
            val s = segments[i]
            val ax = (s.aLon - lon) * mLon
            val ay = (s.aLat - lat) * M_PER_DEG_LAT
            val bx = (s.bLon - lon) * mLon
            val by = (s.bLat - lat) * M_PER_DEG_LAT
            val dx = bx - ax
            val dy = by - ay
            val len2 = dx * dx + dy * dy
            if (len2 < 1e-9) continue

            val t = (((-ax) * dx + (-ay) * dy) / len2).coerceIn(0.0, 1.0)
            val px = ax + t * dx
            val py = ay + t * dy
            val dist = sqrt(px * px + py * py)
            if (dist > radiusM) continue

            for (direction in intArrayOf(1, -1)) {
                // Direction matters as much as distance: the segment nearest a drifted fix is
                // often the right road travelled the wrong way, and starting backwards sends every
                // subsequent step away from the truth.
                val turn = if (courseDeg == null) 0.0
                else abs(bearingDelta(headingOf(i, direction), courseDeg))
                val cost = dist + turn * 0.5
                val incumbent = best
                if (incumbent == null || cost < incumbent.cost) {
                    val off = if (direction > 0) t * lengthsM[i] else (1 - t) * lengthsM[i]
                    best = State(i, off, direction, cost)
                }
            }
        }
        return best
    }

    /**
     * Walk every hypothesis [distanceM] further along the network.
     *
     * This is the operation the whole design exists for: position advances as a scalar along a
     * polyline, so the road supplies the direction and the integrator's heading error stops
     * entering the answer. At each junction the walk forks, and the beam carries the alternatives
     * until one of them is cheap enough to win.
     */
    fun advance(hypotheses: List<State>, distanceM: Double): List<State> {
        val out = ArrayList<State>()
        val stack = ArrayList<State>()
        val remainingOf = ArrayList<Double>()

        for (h in hypotheses) {
            stack.add(h)
            remainingOf.add(distanceM)
            var hops = 0
            while (stack.isNotEmpty() && hops++ < MAX_HOPS) {
                val s = stack.removeAt(stack.size - 1)
                val remaining = remainingOf.removeAt(remainingOf.size - 1)
                val room = lengthsM[s.segment] - s.offsetM
                val next = successors(s.segment, s.direction)
                if (remaining <= room || next.isEmpty()) {
                    out.add(
                        State(
                            s.segment,
                            min(s.offsetM + remaining, lengthsM[s.segment]),
                            s.direction,
                            s.cost,
                        )
                    )
                    continue
                }
                for (n in next) {
                    stack.add(
                        State(n.segment, 0.0, n.direction, s.cost + n.turnDeg * TURN_PENALTY_PER_DEG)
                    )
                    remainingOf.add(remaining - room)
                }
            }
            stack.clear()
            remainingOf.clear()
        }

        out.sortBy { it.cost }
        // Two hypotheses that reached the same segment going the same way are the same hypothesis;
        // keeping both would spend the beam on duplicates instead of on real alternatives.
        val seen = HashSet<Int>()
        val uniq = ArrayList<State>(BEAM)
        for (s in out) {
            if (!seen.add((s.segment shl 1) or (if (s.direction > 0) 0 else 1))) continue
            uniq.add(s)
            if (uniq.size == BEAM) break
        }
        return uniq
    }

    private fun metres(aLat: Double, aLon: Double, bLat: Double, bLon: Double): Double {
        val mLon = M_PER_DEG_LAT * cos(Math.toRadians((aLat + bLat) / 2))
        val dx = (bLon - aLon) * mLon
        val dy = (bLat - aLat) * M_PER_DEG_LAT
        return sqrt(dx * dx + dy * dy)
    }
}
