package com.example.imulogger

import android.app.Application
import android.content.Context
import android.util.Log
import org.osmdroid.mapsforge.MapsForgeTileProvider
import org.osmdroid.mapsforge.MapsForgeTileSource
import org.osmdroid.tileprovider.util.SimpleRegisterReceiver
import org.osmdroid.util.BoundingBox
import org.osmdroid.views.MapView
import java.io.File

/**
 * Offline vector maps rendered on-device from Mapsforge `.map` files.
 *
 * This is the route that needs no tile server at all. A `.map` file is OpenStreetMap data compiled
 * into a vector format, so the phone rasterises it locally — there is no network path to disable,
 * no API key, no usage policy, and no cache to keep warm. It is also far more compact than raster:
 * a vector file covering a whole state is smaller than a tile cache of a single city, which is
 * what makes "preload a radius" moot rather than merely solved.
 *
 * Files go in the same folder as raster archives, and take priority over them when present.
 * Free regional downloads come from OpenAndroMaps.
 */
object MapsforgeSource {

    private const val TAG = "MapsforgeSource"

    private var graphicsInitialised = false

    private const val PREFS = "map_theme"
    private const val KEY_NIGHT = "night"

    /**
     * Day or night styling for the rendered map.
     *
     * Night is the bundled custom theme (dark, with the city labels shrunk); day is Mapsforge's
     * built-in DEFAULT. This is the render theme, not the app theme — the overlay UI stays dark in
     * both, because a light card floating over a light basemap loses all separation.
     */
    fun isNight(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_NIGHT, true)

    fun setNight(context: Context, night: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_NIGHT, night).apply()
    }

    fun mapFiles(context: Context): List<File> =
        OfflineMaps.baseDir(context)
            .listFiles { f -> f.isFile && f.extension.equals("map", ignoreCase = true) }
            ?.sortedBy { it.name }
            ?: emptyList()

    /** What the header of one installed map says about it. */
    data class MapInfo(
        val file: File,
        val bounds: BoundingBox,
        /** When the OSM data in the file was extracted, epoch milliseconds. */
        val dataDateMs: Long,
        val sizeBytes: Long,
    ) {
        fun contains(lat: Double, lon: Double): Boolean = bounds.contains(lat, lon)
    }

    private val infoCache = HashMap<String, MapInfo>()

    /**
     * Read a map's header. Only the header: a few hundred bytes from the front of the file, so it
     * is cheap enough to call from the UI, and the result is cached against the file's size and
     * mtime so a re-download of the same zone invalidates it.
     */
    @Synchronized
    fun info(file: File): MapInfo? {
        val key = "${file.absolutePath}:${file.length()}:${file.lastModified()}"
        infoCache[key]?.let { return it }
        val info = try {
            val mapFile = org.mapsforge.map.reader.MapFile(file)
            try {
                val header = mapFile.mapFileInfo
                val b = header.boundingBox
                MapInfo(
                    file = file,
                    bounds = BoundingBox(b.maxLatitude, b.maxLongitude, b.minLatitude, b.minLongitude),
                    dataDateMs = header.mapDate,
                    sizeBytes = file.length(),
                )
            } finally {
                mapFile.close()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Cannot read header of ${file.name}", e)
            null
        }
        if (info != null) infoCache[key] = info
        return info
    }

    /** The installed map whose extent contains the position, or null if none does. */
    fun covering(context: Context, lat: Double, lon: Double): MapInfo? =
        mapFiles(context).asSequence().mapNotNull { info(it) }.firstOrNull { it.contains(lat, lon) }

    /**
     * Points [map] at the on-device vector maps, returning the area they cover so the caller can
     * frame it. Returns null when there are none, leaving the caller to fall back to raster tiles.
     */
    fun attach(map: MapView): BoundingBox? {
        val context = map.context
        val files = mapFiles(context)
        if (files.isEmpty()) return null

        return try {
            if (!graphicsInitialised) {
                MapsForgeTileSource.createInstance(context.applicationContext as Application)
                graphicsInitialised = true
            }
            val night = isNight(context)
            val source: MapsForgeTileSource = try {
                if (night) {
                    // The bundled theme: dark, with city labels shrunk so they stop dominating.
                    val theme = org.mapsforge.map.android.rendertheme.AssetsRenderTheme(
                        context.applicationContext.assets, "", "custom_theme.xml",
                    )
                    MapsForgeTileSource.createFromFiles(files.toTypedArray(), theme, "custom_theme")
                } else {
                    MapsForgeTileSource.createFromFiles(
                        files.toTypedArray(),
                        org.mapsforge.map.rendertheme.InternalRenderTheme.DEFAULT,
                        "default",
                    )
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to load render theme, falling back to built-in default", e)
                MapsForgeTileSource.createFromFiles(files.toTypedArray())
            }
            
            map.tileProvider = MapsForgeTileProvider(SimpleRegisterReceiver(context), source, null)
            // A vector map is inherently offline; there is no server behind it to consult.
            map.setUseDataConnection(false)
            Log.i(TAG, "Rendering from ${files.size} Mapsforge file(s): ${files.joinToString { it.name }}")
            source.boundsOsmdroid
        } catch (e: Exception) {
            // A corrupt or wrong-version .map file should degrade to raster tiles, not take the
            // map down with it.
            Log.e(TAG, "Could not open Mapsforge files, falling back to raster tiles", e)
            null
        }
    }
}
