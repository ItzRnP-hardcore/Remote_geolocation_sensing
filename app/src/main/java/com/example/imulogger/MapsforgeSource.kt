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

    fun mapFiles(context: Context): List<File> =
        OfflineMaps.baseDir(context)
            .listFiles { f -> f.isFile && f.extension.equals("map", ignoreCase = true) }
            ?.sortedBy { it.name }
            ?: emptyList()

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
            var source: MapsForgeTileSource? = null
            try {
                // Use the built-in theme which has all patterns and symbols
                val theme = org.mapsforge.map.rendertheme.InternalRenderTheme.OSMARENDER
                source = MapsForgeTileSource.createFromFiles(files.toTypedArray(), theme, "osmarender")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to load InternalRenderTheme, falling back to default", e)
                source = MapsForgeTileSource.createFromFiles(files.toTypedArray())
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
