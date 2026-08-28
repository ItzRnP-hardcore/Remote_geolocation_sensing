package com.example.imulogger

import android.content.Context
import org.osmdroid.config.Configuration
import org.osmdroid.tileprovider.MapTileProviderBasic
import org.osmdroid.views.MapView
import java.io.File

/**
 * Tile setup for the map.
 *
 * osmdroid resolves tiles in two stages: it first looks inside any archive file sitting in
 * [baseDir], and only falls back to the network if [MapView.setUseDataConnection] allows it. That
 * ordering is what makes a genuinely offline map possible — with the data connection disabled the
 * archive is the only source, so the map behaves identically in a tunnel and on the surface.
 *
 * Supported archive formats are whatever osmdroid's ArchiveFileFactory recognises: .mbtiles,
 * .sqlite, .gpkg, .gemf and plain .zip tile trees.
 */
object OfflineMaps {

    private val ARCHIVE_EXTENSIONS = setOf("mbtiles", "sqlite", "gpkg", "gemf", "zip")

    /** Where to drop offline archives. Deliberately app-scoped, so no storage permission is needed. */
    fun baseDir(context: Context): File =
        File(context.getExternalFilesDir(null) ?: context.filesDir, "osmdroid")

    fun archives(context: Context): List<File> =
        baseDir(context).listFiles { f ->
            f.isFile && f.extension.lowercase() in ARCHIVE_EXTENSIONS
        }?.sortedBy { it.name } ?: emptyList()

    /**
     * Must run before any MapView is inflated — osmdroid reads this configuration during view
     * construction, and a MapView built before it lands writes its cache to the legacy
     * /sdcard/osmdroid path, which modern scoped storage will not grant.
     */
    fun configure(context: Context) {
        val cfg = Configuration.getInstance()
        cfg.load(context, context.getSharedPreferences("osmdroid", Context.MODE_PRIVATE))
        // OSM's tile policy requires an identifying agent; the default string is blocked outright.
        cfg.userAgentValue = context.packageName
        val base = baseDir(context)
        base.mkdirs()
        cfg.osmdroidBasePath = base
        cfg.osmdroidTileCache = File(base, "tiles").apply { mkdirs() }
    }

    /**
     * Resolution order is deliberate: on-device vector maps beat everything, because they need no
     * network at all and no cache to be warm. Raster archives and the tile cache come next, and
     * the network only if the map is not in offline mode.
     */
    fun apply(map: MapView, offline: Boolean): org.osmdroid.util.BoundingBox? {
        MapsforgeSource.attach(map)?.let { return it }

        // attach() may have swapped in a Mapsforge provider on an earlier call; put the raster
        // provider back before setting a raster source on it.
        if (map.tileProvider !is MapTileProviderBasic) {
            map.tileProvider = MapTileProviderBasic(map.context)
        }
        map.setTileSource(TileSources.current(map.context))
        map.setUseDataConnection(!offline)
        return null
    }

    /** True when the map is drawing from on-device vector data rather than tiles. */
    fun usingVectorMaps(context: Context): Boolean = MapsforgeSource.mapFiles(context).isNotEmpty()
}
