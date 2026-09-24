package com.example.imulogger

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import org.osmdroid.util.GeoPoint
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.Locale
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * High-performance navigation router supporting offline A* pathfinding over Mapsforge road graphs,
 * Nominatim place search / geocoding, and OSRM online routing fallback.
 */
object NavigationRouter {

    private const val TAG = "NavigationRouter"
    private const val NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    private const val OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
    private const val USER_AGENT = "IMULogger-Navigation/1.0 (Android Geosensing)"

    data class SearchResult(
        val title: String,
        val subtitle: String,
        val lat: Double,
        val lon: Double,
        val distanceM: Double? = null,
    )

    data class NavRoute(
        val points: List<GeoPoint>,
        val distanceM: Double,
        val durationSeconds: Double,
        val isOffline: Boolean,
        val destinationName: String? = null,
    )

    /**
     * Search for places / addresses via OpenStreetMap Nominatim.
     */
    suspend fun searchPlaces(
        query: String,
        userLat: Double? = null,
        userLon: Double? = null,
    ): List<SearchResult> = withContext(Dispatchers.IO) {
        val trimmed = query.trim()
        if (trimmed.length < 2) return@withContext emptyList()

        val results = ArrayList<SearchResult>()
        var conn: HttpURLConnection? = null
        try {
            val encoded = URLEncoder.encode(trimmed, "UTF-8")
            val urlStr = "$NOMINATIM_URL?format=json&q=$encoded&limit=6&addressdetails=1"
            val url = URL(urlStr)
            conn = (url.openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("User-Agent", USER_AGENT)
                setRequestProperty("Accept", "application/json")
                connectTimeout = 5000
                readTimeout = 5000
            }

            if (conn.responseCode == HttpURLConnection.HTTP_OK) {
                val reader = BufferedReader(InputStreamReader(conn.inputStream))
                val response = reader.use { it.readText() }
                val array = JSONArray(response)

                for (i in 0 until array.length()) {
                    val obj = array.getJSONObject(i)
                    val displayName = obj.optString("display_name", "")
                    val lat = obj.optDouble("lat", Double.NaN)
                    val lon = obj.optDouble("lon", Double.NaN)
                    if (lat.isNaN() || lon.isNaN()) continue

                    val parts = displayName.split(",", limit = 2)
                    val title = parts.getOrNull(0)?.trim() ?: displayName
                    val subtitle = parts.getOrNull(1)?.trim() ?: ""

                    val dist = if (userLat != null && userLon != null) {
                        haversine(userLat, userLon, lat, lon)
                    } else null

                    results.add(SearchResult(title, subtitle, lat, lon, dist))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Search failed for '$query'", e)
        } finally {
            conn?.disconnect()
        }

        // Sort by distance if user location is available
        if (userLat != null && userLon != null) {
            results.sortBy { it.distanceM ?: Double.MAX_VALUE }
        }
        results
    }

    /**
     * Compute shortest drivable path between [start] and [dest].
     *
     * Tries offline A* over a corridor-based road graph first — the corridor spans the full
     * bounding box between start and destination at z13–z15 depending on distance, so routes
     * up to 200+ km are resolved entirely on-device from the installed Mapsforge `.map` file.
     * Falls back to OSRM only if the offline graph yields no path (e.g., destination is outside
     * the installed map coverage).
     */
    suspend fun calculateRoute(
        start: GeoPoint,
        dest: GeoPoint,
        roadNetwork: RoadNetwork? = null,
        destinationName: String? = null,
    ): NavRoute? = withContext(Dispatchers.IO) {
        // 1. Offline A* over a corridor graph spanning start → dest
        if (roadNetwork != null) {
            try {
                val graph = roadNetwork.graphForRoute(
                    start.latitude, start.longitude,
                    dest.latitude, dest.longitude,
                )
                if (graph != null) {
                    val offlineResult = graph.findPath(
                        start.latitude, start.longitude,
                        dest.latitude, dest.longitude,
                        snapRadiusM = 1000.0,   // wider snap for long-range endpoint matching
                    )
                    if (offlineResult != null && offlineResult.points.isNotEmpty()) {
                        val geoPoints = offlineResult.points.map { GeoPoint(it.lat, it.lon) }
                        // Longer routes use highways; scale assumed speed accordingly.
                        val speedAssumedMps = when {
                            offlineResult.distanceM < 10_000  -> 10.0   // ~36 km/h city
                            offlineResult.distanceM < 50_000  -> 14.0   // ~50 km/h suburban
                            else                              -> 18.0   // ~65 km/h highway
                        }
                        val dur = offlineResult.distanceM / speedAssumedMps
                        Log.i(TAG, "Found offline A* route: ${offlineResult.distanceM.toInt()}m, " +
                                "${graph.segments.size} segments, ${graph.nodeCount} nodes")
                        return@withContext NavRoute(
                            points = geoPoints,
                            distanceM = offlineResult.distanceM,
                            durationSeconds = dur,
                            isOffline = true,
                            destinationName = destinationName,
                        )
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "Offline route search error, trying online fallback", e)
            }
        }

        // 2. Online OSRM driving route fallback
        return@withContext fetchOsrmRoute(start, dest, destinationName)
    }

    private fun fetchOsrmRoute(
        start: GeoPoint,
        dest: GeoPoint,
        destinationName: String?,
    ): NavRoute? {
        var conn: HttpURLConnection? = null
        return try {
            val urlStr = String.format(
                Locale.US,
                "%s/%.6f,%.6f;%.6f,%.6f?overview=full&geometries=geojson",
                OSRM_URL,
                start.longitude, start.latitude,
                dest.longitude, dest.latitude,
            )
            val url = URL(urlStr)
            conn = (url.openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("User-Agent", USER_AGENT)
                setRequestProperty("Accept", "application/json")
                connectTimeout = 6000
                readTimeout = 6000
            }

            if (conn.responseCode == HttpURLConnection.HTTP_OK) {
                val response = conn.inputStream.bufferedReader().use { it.readText() }
                val root = JSONObject(response)
                val code = root.optString("code", "")
                if (code == "Ok") {
                    val routes = root.getJSONArray("routes")
                    if (routes.length() > 0) {
                        val r = routes.getJSONObject(0)
                        val dist = r.optDouble("distance", 0.0)
                        val dur = r.optDouble("duration", 0.0)
                        val geometry = r.getJSONObject("geometry")
                        val coords = geometry.getJSONArray("coordinates")
                        val points = ArrayList<GeoPoint>(coords.length())
                        for (i in 0 until coords.length()) {
                            val c = coords.getJSONArray(i)
                            val lon = c.getDouble(0)
                            val lat = c.getDouble(1)
                            points.add(GeoPoint(lat, lon))
                        }
                        Log.i(TAG, "Found OSRM route: ${dist.toInt()}m, ${points.size} points")
                        return NavRoute(
                            points = points,
                            distanceM = dist,
                            durationSeconds = dur,
                            isOffline = false,
                            destinationName = destinationName,
                        )
                    }
                }
            }
            null
        } catch (e: Exception) {
            Log.w(TAG, "OSRM routing failed", e)
            null
        } finally {
            conn?.disconnect()
        }
    }

    fun haversine(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6371000.0 // Earth radius in meters
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = sin(dLat / 2) * sin(dLat / 2) +
            cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) *
            sin(dLon / 2) * sin(dLon / 2)
        val c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return r * c
    }
}
