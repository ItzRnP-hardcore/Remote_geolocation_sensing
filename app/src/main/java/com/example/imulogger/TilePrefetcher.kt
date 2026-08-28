package com.example.imulogger

import android.content.Context
import android.util.Log
import org.osmdroid.tileprovider.cachemanager.CacheManager
import org.osmdroid.util.BoundingBox
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import kotlin.math.abs
import kotlin.math.cos

/**
 * Downloads a disc of tiles around a point into osmdroid's on-device cache, so the map keeps
 * working with the radio off.
 *
 * The cache osmdroid writes here is the same store the offline mode reads from, which is the whole
 * point: there is no separate "offline map" to manage, only a cache that is either warm or not.
 *
 * A note on the tile source. OSM's public tile servers are run on donated capacity and their usage
 * policy asks that clients not bulk-download. A one-off disc around a route for personal research
 * is the scale that policy tolerates; sweeping a city at high zoom is not. [MAX_REASONABLE_TILES]
 * enforces that boundary rather than leaving it to judgement. To go bigger, point osmdroid at a
 * tile source you are entitled to bulk-fetch from.
 */
object TilePrefetcher {

    private const val TAG = "TilePrefetcher"

    /** Driving-useful zooms: z12 gives regional context, z16 shows individual streets. */
    const val ZOOM_MIN = 12
    const val ZOOM_MAX = 16

    /** Roughly a 10 km radius at z16. Beyond this we are bulk-downloading, not caching a route. */
    private const val MAX_REASONABLE_TILES = 4000

    data class Progress(val done: Int, val total: Int, val finished: Boolean, val message: String?)

    fun boundingBox(centre: GeoPoint, radiusKm: Double): BoundingBox {
        val dLat = radiusKm / 111.32
        // Longitude degrees shrink with latitude, so a fixed degree box would be too narrow far
        // from the equator and absurdly wide near the poles.
        val dLon = radiusKm / (111.32 * cos(Math.toRadians(centre.latitude)).coerceAtLeast(0.01))
        return BoundingBox(
            centre.latitude + dLat,
            centre.longitude + dLon,
            centre.latitude - dLat,
            centre.longitude - dLon,
        )
    }

    fun estimateTiles(map: MapView, centre: GeoPoint, radiusKm: Double): Int =
        try {
            CacheManager(map).possibleTilesInArea(boundingBox(centre, radiusKm), ZOOM_MIN, ZOOM_MAX)
        } catch (e: Exception) {
            Log.w(TAG, "Could not estimate tile count", e)
            -1
        }

    /**
     * Starts the download. [onProgress] is invoked on the main thread. Returns the estimated tile
     * count, or -1 if the request was refused for being too large.
     */
    fun prefetch(
        context: Context,
        map: MapView,
        centre: GeoPoint,
        radiusKm: Double,
        onProgress: (Progress) -> Unit,
    ): Int {
        // osmdroid throws TileSourcePolicyException from inside CacheManager's AsyncTask when the
        // source forbids bulk download, which crashes the process rather than surfacing an error.
        // Check the policy up front so the refusal is a message instead of a fatal exception.
        val source = map.tileProvider.tileSource
        if (source !is org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase ||
            !TileSources.acceptsBulkDownload(source)
        ) {
            onProgress(
                Progress(
                    0, 0, true,
                    "This tile source cannot be bulk-downloaded. OpenStreetMap's public servers " +
                        "forbid it, and osmdroid enforces that. Set a tile URL template from a " +
                        "provider whose terms allow caching, then preload again.",
                )
            )
            return -1
        }

        val box = boundingBox(centre, radiusKm)
        val manager = CacheManager(map)
        val total = try {
            manager.possibleTilesInArea(box, ZOOM_MIN, ZOOM_MAX)
        } catch (e: Exception) {
            Log.w(TAG, "Tile estimate failed", e)
            0
        }
        if (total > MAX_REASONABLE_TILES) {
            onProgress(
                Progress(
                    0, total, true,
                    "$total tiles is past what the public OSM servers should be asked for in one " +
                        "go. Reduce the radius, or switch to a tile source you can bulk-fetch from.",
                )
            )
            return -1
        }

        // CacheManager pulls tiles through the MapView's own provider, so it inherits the offline
        // flag: with the map offline the download silently fetches nothing at all. Lift the flag
        // for the duration and put it back afterwards, so prefetching from an offline map works
        // without leaving the map online once it finishes.
        val wasOnline = map.useDataConnection()
        map.setUseDataConnection(true)
        fun restore() = map.setUseDataConnection(wasOnline)

        manager.downloadAreaAsync(
            context,
            box,
            ZOOM_MIN,
            ZOOM_MAX,
            object : CacheManager.CacheManagerCallback {
                override fun onTaskComplete() {
                    restore()
                    onProgress(Progress(total, total, true, "Cached $total tiles"))
                }

                override fun onTaskFailed(errors: Int) {
                    restore()
                    onProgress(
                        Progress(
                            total, total, true,
                            "Finished with $errors tile errors — usually means the network dropped.",
                        )
                    )
                }

                override fun updateProgress(progress: Int, currentZoomLevel: Int, zoomMin: Int, zoomMax: Int) {
                    onProgress(Progress(progress, total, false, "z$currentZoomLevel"))
                }

                override fun downloadStarted() {
                    onProgress(Progress(0, total, false, "Starting"))
                }

                override fun setPossibleTilesInArea(total: Int) = Unit
            },
        )
        return total
    }

    fun movedFarEnoughToRefresh(from: GeoPoint?, to: GeoPoint, radiusKm: Double): Boolean {
        if (from == null) return true
        // Refresh once the vehicle has eaten half the cached disc, so the edge is never reached
        // with an empty cache in front of it.
        val km = from.distanceToAsDouble(to) / 1000.0
        return abs(km) > radiusKm / 2
    }
}
