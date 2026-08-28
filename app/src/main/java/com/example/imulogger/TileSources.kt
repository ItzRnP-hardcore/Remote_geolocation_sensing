package com.example.imulogger

import android.content.Context
import org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase
import org.osmdroid.tileprovider.tilesource.TileSourceFactory
import org.osmdroid.tileprovider.tilesource.TileSourcePolicy
import org.osmdroid.util.MapTileIndex

/**
 * Which server the map draws from, and whether that server may be bulk-downloaded.
 *
 * This distinction is the whole reason this file exists. osmdroid tags every built-in source with
 * a [TileSourcePolicy], and the default Mapnik source carries `FLAG_NO_BULK` because OSM's public
 * tile servers run on donated capacity and their usage policy forbids bulk downloading. osmdroid
 * enforces that by throwing from CacheManager, so "preload a 10 km radius" is simply not available
 * against the default source — not as a limitation of this app, but as the correct behaviour.
 *
 * To preload, point this at a source you are entitled to bulk-fetch from. Free tiers that issue a
 * key without a payment card include Thunderforest and Stadia Maps; a self-hosted or vendor-issued
 * endpoint works equally well. Whatever you use, check its terms cover caching before enabling it.
 */
object TileSources {

    private const val PREFS = "tiles"
    private const val KEY_TEMPLATE = "url_template"

    /** Template placeholders are the usual slippy-map ones: {z}, {x}, {y}. */
    fun template(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_TEMPLATE, null)
            ?.takeIf { it.isNotBlank() }

    fun setTemplate(context: Context, template: String?) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_TEMPLATE, template?.trim())
            .apply()
    }

    /**
     * The source to draw with. Falls back to Mapnik, which renders fine but cannot be prefetched.
     */
    fun current(context: Context): OnlineTileSourceBase {
        val t = template(context) ?: return TileSourceFactory.MAPNIK
        return TemplatedTileSource("custom", t)
    }

    fun acceptsBulkDownload(source: OnlineTileSourceBase): Boolean =
        source.tileSourcePolicy.acceptsBulkDownload()

    /**
     * A tile source built from a URL template, declared bulk-capable.
     *
     * The policy here is an assertion by whoever pasted the template that their provider permits
     * caching — osmdroid has no way to verify it, and neither does this app.
     */
    private class TemplatedTileSource(name: String, private val template: String) :
        OnlineTileSourceBase(
            name,
            3,
            19,
            256,
            ".png",
            arrayOf(template),
            "© OpenStreetMap contributors",
            TileSourcePolicy(
                2,
                TileSourcePolicy.FLAG_NO_PREVENTIVE or
                    TileSourcePolicy.FLAG_USER_AGENT_MEANINGFUL,
            ),
        ) {
        override fun getTileURLString(pMapTileIndex: Long): String =
            template
                .replace("{z}", MapTileIndex.getZoom(pMapTileIndex).toString())
                .replace("{x}", MapTileIndex.getX(pMapTileIndex).toString())
                .replace("{y}", MapTileIndex.getY(pMapTileIndex).toString())
    }
}
