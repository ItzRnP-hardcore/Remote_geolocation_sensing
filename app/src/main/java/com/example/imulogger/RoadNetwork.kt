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
 * What the format does *not* give is topology: ways are clipped at tile boundaries and carry no
 * node identity, so there is no connectivity to route over. [MapMatcher] is built to not need it.
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

    private fun tileSegments(store: MapFile, x: Int, y: Int): List<Segment> {
        val key = (x.toLong() shl 32) or (y.toLong() and 0xffffffffL)
        tiles[key]?.let { return it }

        val segments = ArrayList<Segment>()
        try {
            val result = store.readMapData(Tile(x, y, ZOOM, TILE_SIZE))
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
                if (cls !in DRIVABLE) continue

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
            Log.w(TAG, "Could not read tile $x/$y", e)
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
