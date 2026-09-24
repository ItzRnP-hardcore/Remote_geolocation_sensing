package com.example.imulogger

import android.util.Log
import org.mapsforge.core.model.Tile
import org.mapsforge.map.reader.MapFile
import java.io.File
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt
import kotlin.math.tan

/**
 * Drivable road geometry, read straight out of the Mapsforge map already on the device.
 *
 * The `.map` file is built for rendering, but every way keeps its OSM tags, so `highway=*`
 * centrelines come back from [MapFile.readMapData] with full coordinates. That means the road
 * network needs no extra download, no `.osm.pbf`, and no separate routing graph — a probe of one
 * z15 tile over IIT Kharagpur returned 106 ways and 660 vertices.
 *
 * What the format does *not* give directly is topology: ways are clipped at tile boundaries and
 * carry no node identity, so there is no connectivity to route over. [MapMatcher] is built to not
 * need it; [graphNear] recovers it anyway, by snapping coincident endpoints together.
 *
 * Not thread-safe. Confined to the matcher thread in [SensorService].
 */
class RoadNetwork(private val mapFile: File) {

    private companion object {
        const val TAG = "RoadNetwork"

        /**
         * Roads are read at z15. Lower zooms are generalised for rendering — vertices get dropped
         * and short links disappear — which would snap the vehicle to a simplified caricature of
         * the road it is actually on.
         */
        const val ZOOM: Byte = 15
        const val TILE_SIZE = 256

        /** Cheap equirectangular scale; accurate to centimetres over the few km we ever query. */
        const val M_PER_DEG_LAT = 111_132.0

        /**
         * Ways a car can be on. Footways and cycleways are excluded deliberately: including them
         * gives the matcher tempting parallel candidates a few metres from the carriageway.
         */
        val DRIVABLE = setOf(
            "motorway", "motorway_link", "trunk", "trunk_link",
            "primary", "primary_link", "secondary", "secondary_link",
            "tertiary", "tertiary_link", "unclassified", "residential",
            "living_street", "service", "road",
        )

        /**
         * Major roads only — for long-range routing (> 20 km) where residential streets would
         * bloat the graph without contributing to inter-city paths.
         */
        val DRIVABLE_TRUNK = setOf(
            "motorway", "motorway_link", "trunk", "trunk_link",
            "primary", "primary_link", "secondary", "secondary_link",
            "tertiary", "tertiary_link",
        )

        /** z15 tile radius (~1.1 km each) loaded around start/dest for last-mile resolution. */
        private const val ENDPOINT_TILE_RADIUS = 3
    }

    /** One straight piece of road, pre-resolved to the numbers the matcher needs. */
    class Segment(
        val aLat: Double, val aLon: Double,
        val bLat: Double, val bLon: Double,
        val bearingDeg: Double,
        val roadClass: String,
        val oneway: Boolean,
    )

    /** A projected position on a segment, with how far off it was. */
    class Candidate(
        val segment: Segment,
        val lat: Double,
        val lon: Double,
        val distanceM: Double,
    )

    private var store: MapFile? = null
    private val tiles = HashMap<Long, List<Segment>>()
    private var cachedGraph: RoadGraph? = null
    private var cachedGraphKey = 0L

    fun open(): Boolean = try {
        store = MapFile(mapFile)
        Log.i(TAG, "Opened ${mapFile.name}, bbox ${store?.boundingBox()}")
        true
    } catch (e: Exception) {
        Log.e(TAG, "Could not open ${mapFile.name}", e)
        false
    }

    fun close() {
        try {
            store?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing map file", e)
        }
        store = null
        tiles.clear()
        cachedGraph = null
        cachedGraphKey = 0L
    }

    /**
     * Segments within [radiusM] of a point, already projected.
     *
     * Loads the 3x3 tile block around the point so a vehicle near a tile edge still sees the road
     * continuing on the other side. Tiles are cached; at z15 each covers roughly 1 km here, so a
     * whole session usually touches a handful.
     */
    fun candidatesNear(lat: Double, lon: Double, radiusM: Double): List<Candidate> {
        val store = this.store ?: return emptyList()
        val tx = lonToTileX(lon, ZOOM)
        val ty = latToTileY(lat, ZOOM)

        val out = ArrayList<Candidate>()
        val mPerDegLon = M_PER_DEG_LAT * cos(Math.toRadians(lat))
        for (dx in -1..1) for (dy in -1..1) {
            for (seg in tileSegments(store, tx + dx, ty + dy)) {
                val c = project(seg, lat, lon, mPerDegLon) ?: continue
                if (c.distanceM <= radiusM) out.add(c)
            }
        }
        return out
    }

    /**
     * A connected [RoadGraph] over the 3x3 tile block around a point.
     *
     * Built lazily and cached against the centre tile, because rebuilding costs a pass over a few
     * thousand segments and the vehicle stays inside one z15 tile (about 1 km here) for a long
     * time. Callers must treat segment indices as valid only for the instance they were given:
     * crossing into a new block yields a different graph, and [AlongRoadTracker] re-localises when
     * it sees one.
     */
    fun graphNear(lat: Double, lon: Double): RoadGraph? {
        val store = this.store ?: return null
        val tx = lonToTileX(lon, ZOOM)
        val ty = latToTileY(lat, ZOOM)
        val key = (tx.toLong() shl 32) or (ty.toLong() and 0xffffffffL)
        cachedGraph?.let { if (cachedGraphKey == key) return it }

        val segments = ArrayList<Segment>()
        for (dx in -1..1) for (dy in -1..1) {
            segments.addAll(tileSegments(store, tx + dx, ty + dy))
        }
        if (segments.isEmpty()) return null

        val graph = RoadGraph(segments)
        Log.i(TAG, "Graph at $tx/$ty: ${segments.size} segments, ${graph.nodeCount} nodes")
        cachedGraph = graph
        cachedGraphKey = key
        return graph
    }

    /**
     * Build a [RoadGraph] covering the corridor between two points, for long-range routing.
     *
     * Strategy by distance:
     * - **< 10 km**: z15 bounding box, all drivable roads (same quality as [graphNear]).
     * - **10–50 km**: z14 corridor with 8 km margins, major roads only in the trunk.
     * - **> 50 km**: z13 corridor with 20 km margins, major roads only in the trunk.
     *
     * In every case z15 tiles are loaded within [ENDPOINT_TILE_RADIUS] of both endpoints so the
     * A* can reach the user's actual residential street. The z13/z14 trunk tiles contain
     * motorways through tertiaries, which is exactly the road class a 200 km drive uses.
     *
     * Memory: a 200 km corridor at z13 ±20 km loads roughly 200–500 tiles, yielding ~50K–150K
     * segments (~15–25 MB). Graph construction plus A* completes in 1–3 seconds on a modern
     * phone — visible but tolerable for a one-off route computation.
     */
    fun graphForRoute(
        startLat: Double, startLon: Double,
        destLat: Double, destLon: Double,
    ): RoadGraph? {
        val store = this.store ?: return null
        val directDistM = haversineM(startLat, startLon, destLat, destLon)

        // Trunk zoom and corridor width, chosen so the tile count stays under ~600.
        val trunkZoom: Byte
        val corridorM: Double
        val trunkFilter: Set<String>
        when {
            directDistM < 10_000  -> { trunkZoom = 15; corridorM =  3_000.0; trunkFilter = DRIVABLE }
            directDistM < 50_000  -> { trunkZoom = 14; corridorM =  8_000.0; trunkFilter = DRIVABLE_TRUNK }
            directDistM < 150_000 -> { trunkZoom = 13; corridorM = 15_000.0; trunkFilter = DRIVABLE_TRUNK }
            else                  -> { trunkZoom = 13; corridorM = 20_000.0; trunkFilter = DRIVABLE_TRUNK }
        }

        val midLat = (startLat + destLat) / 2.0
        val mPerDegLon = M_PER_DEG_LAT * cos(Math.toRadians(midLat))
        val marginLat = corridorM / M_PER_DEG_LAT
        val marginLon = corridorM / mPerDegLon

        val minLat = min(startLat, destLat) - marginLat
        val maxLat = max(startLat, destLat) + marginLat
        val minLon = min(startLon, destLon) - marginLon
        val maxLon = max(startLon, destLon) + marginLon

        val minTx = lonToTileX(minLon, trunkZoom)
        val maxTx = lonToTileX(maxLon, trunkZoom)
        val minTy = latToTileY(maxLat, trunkZoom)   // y is inverted in slippy-map convention
        val maxTy = latToTileY(minLat, trunkZoom)

        val tileCount = (maxTx - minTx + 1).toLong() * (maxTy - minTy + 1)
        Log.i(TAG, "Route graph: ${directDistM.toInt()}m, z$trunkZoom, " +
                "${maxTx - minTx + 1}x${maxTy - minTy + 1} = $tileCount tiles, " +
                "corridor ${corridorM.toInt()}m")

        val segments = ArrayList<Segment>()

        // 1. Load trunk corridor tiles at the chosen zoom.
        for (tx in minTx..maxTx) {
            for (ty in minTy..maxTy) {
                segments.addAll(tileSegmentsAt(store, tx, ty, trunkZoom, trunkFilter))
            }
        }

        // 2. Overlay z15 detail around both endpoints (all road classes) so the A* can
        //    reach residential streets at the start and destination.
        if (trunkZoom != ZOOM) {
            val seen = HashSet<Long>()
            for ((eLat, eLon) in listOf(startLat to startLon, destLat to destLon)) {
                val cx = lonToTileX(eLon, ZOOM)
                val cy = latToTileY(eLat, ZOOM)
                for (dx in -ENDPOINT_TILE_RADIUS..ENDPOINT_TILE_RADIUS) {
                    for (dy in -ENDPOINT_TILE_RADIUS..ENDPOINT_TILE_RADIUS) {
                        val tk = ((cx + dx).toLong() shl 32) or ((cy + dy).toLong() and 0xffffffffL)
                        if (!seen.add(tk)) continue
                        segments.addAll(tileSegmentsAt(store, cx + dx, cy + dy, ZOOM, DRIVABLE))
                    }
                }
            }
        }

        if (segments.isEmpty()) return null
        val graph = RoadGraph(segments)
        Log.i(TAG, "Route graph built: ${segments.size} segments, ${graph.nodeCount} nodes")
        return graph
    }

    private fun tileSegments(store: MapFile, x: Int, y: Int): List<Segment> =
        tileSegmentsAt(store, x, y, ZOOM, DRIVABLE)

    /**
     * Read drivable road segments from one tile at an arbitrary zoom level, filtered to [filter].
     *
     * Results are cached per (x, y, zoom) triple, so repeated calls for overlapping corridors or
     * endpoint patches do not re-parse the binary tile data.
     */
    private fun tileSegmentsAt(
        store: MapFile, x: Int, y: Int, z: Byte, filter: Set<String>,
    ): List<Segment> {
        // Incorporate zoom into the cache key so z13 and z15 reads of overlapping areas
        // do not collide.  The zoom byte goes into the top 8 bits of the long.
        val key = (z.toLong() shl 56) or
                ((x.toLong() and 0xfffffffL) shl 28) or
                (y.toLong() and 0xfffffffL)
        tiles[key]?.let { return it }

        val segments = ArrayList<Segment>()
        try {
            val result = store.readMapData(Tile(x, y, z, TILE_SIZE))
            for (way in result.ways) {
                var highway: String? = null
                var oneway = false
                for (tag in way.tags) {
                    when (tag.key) {
                        "highway" -> highway = tag.value
                        "oneway" -> oneway = tag.value == "yes" || tag.value == "true" || tag.value == "1"
                    }
                }
                val cls = highway ?: continue
                if (cls !in filter) continue

                for (block in way.latLongs) {
                    for (i in 0 until block.size - 1) {
                        val a = block[i]
                        val b = block[i + 1]
                        segments.add(
                            Segment(
                                a.latitude, a.longitude, b.latitude, b.longitude,
                                bearing(a.latitude, a.longitude, b.latitude, b.longitude),
                                cls, oneway,
                            )
                        )
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not read tile z$z $x/$y", e)
        }
        tiles[key] = segments
        return segments
    }

    /** Perpendicular projection of a point onto a segment, clamped to the segment's ends. */
    private fun project(seg: Segment, lat: Double, lon: Double, mPerDegLon: Double): Candidate? {
        val ax = (seg.aLon - lon) * mPerDegLon
        val ay = (seg.aLat - lat) * M_PER_DEG_LAT
        val bx = (seg.bLon - lon) * mPerDegLon
        val by = (seg.bLat - lat) * M_PER_DEG_LAT

        val dx = bx - ax
        val dy = by - ay
        val len2 = dx * dx + dy * dy
        if (len2 < 1e-9) return null

        // t is where along AB the perpendicular from the point lands; clamping keeps the match on
        // the segment rather than on its infinite extension.
        val t = (((-ax) * dx + (-ay) * dy) / len2).coerceIn(0.0, 1.0)
        val px = ax + t * dx
        val py = ay + t * dy
        val dist = sqrt(px * px + py * py)

        return Candidate(
            seg,
            lat + py / M_PER_DEG_LAT,
            lon + px / mPerDegLon,
            dist,
        )
    }

    private fun bearing(aLat: Double, aLon: Double, bLat: Double, bLon: Double): Double {
        val mPerDegLon = M_PER_DEG_LAT * cos(Math.toRadians((aLat + bLat) / 2))
        val east = (bLon - aLon) * mPerDegLon
        val north = (bLat - aLat) * M_PER_DEG_LAT
        val deg = Math.toDegrees(atan2(east, north))
        return if (deg < 0) deg + 360 else deg
    }

    private fun lonToTileX(lon: Double, z: Byte): Int =
        floor((lon + 180.0) / 360.0 * (1 shl z.toInt())).toInt()

    private fun latToTileY(lat: Double, z: Byte): Int {
        val r = Math.toRadians(lat)
        return floor((1.0 - ln(tan(r) + 1.0 / cos(r)) / Math.PI) / 2.0 * (1 shl z.toInt())).toInt()
    }

    private fun haversineM(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = kotlin.math.sin(dLat / 2).let { it * it } +
            cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) *
            kotlin.math.sin(dLon / 2).let { it * it }
        return 6_371_000.0 * 2 * atan2(sqrt(a), sqrt(1 - a))
    }
}

/** Smallest signed difference between two bearings, in degrees, in [-180, 180]. */
fun bearingDelta(a: Double, b: Double): Double {
    var d = (a - b) % 360.0
    if (d > 180) d -= 360.0
    if (d < -180) d += 360.0
    return d
}

/** Absolute bearing difference treating a road as undirected, so 179 degrees becomes 1. */
fun undirectedBearingDelta(a: Double, b: Double): Double {
    val d = abs(bearingDelta(a, b))
    return if (d > 90) 180 - d else d
}
