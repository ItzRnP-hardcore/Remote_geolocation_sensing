package com.example.imulogger

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.util.Log
import java.io.File

/**
 * Fetches Mapsforge regional maps onto the device.
 *
 * Downloads run through the system [DownloadManager] rather than an in-app HTTP call: these files
 * are 100–500 MB, the user will background the app long before one finishes, and DownloadManager
 * already handles the notification, the resume (the server sends `Accept-Ranges: bytes`), and
 * surviving process death. An in-app downloader would have to reimplement all three.
 *
 * Files land as `<zone>.map.part` and are renamed to `<zone>.map` only after the header check
 * passes. [MapsforgeSource] only looks for `.map`, so a download in flight — or one that arrived
 * truncated — is never handed to the renderer.
 */
object MapDownloader {

    private const val TAG = "MapDownloader"
    private const val BASE_URL = "https://download.mapsforge.org/maps/v5/asia/india/"
    private const val PREFS = "map_downloads"

    /** Every Mapsforge `.map` file starts with this; a truncated or error-page download will not. */
    private val MAGIC = "mapsforge binary OSM".toByteArray(Charsets.US_ASCII)

    data class Zone(val id: String, val label: String, val approxMb: Int)

    /**
     * Mapsforge splits India into zones rather than states. Sizes are from the server and drift
     * a little as OSM data grows; they are here to set expectations before a half-gigabyte
     * download starts, not to validate anything.
     */
    val INDIA_ZONES = listOf(
        Zone("eastern-zone", "Eastern (WB, Jharkhand, Odisha, Bihar)", 210),
        Zone("northern-zone", "Northern (Delhi, UP, Punjab, Rajasthan)", 205),
        Zone("western-zone", "Western (Maharashtra, Gujarat, Goa)", 203),
        Zone("central-zone", "Central (MP, Chhattisgarh)", 313),
        Zone("southern-zone", "Southern (KA, TN, KL, AP, TS)", 520),
        Zone("north-eastern-zone", "North-eastern (Assam and the seven sisters)", 112),
    )

    fun installedFile(context: Context, zone: Zone): File =
        File(OfflineMaps.baseDir(context), "${zone.id}.map")

    fun partFile(context: Context, zone: Zone): File =
        File(OfflineMaps.baseDir(context), "${zone.id}.map.part")

    fun isInstalled(context: Context, zone: Zone): Boolean = installedFile(context, zone).isFile

    // ------------------------------------------------------------------ enqueue

    /**
     * Start a download. [wifiOnly] is offered as an explicit choice rather than a hidden default,
     * because half a gigabyte over a metered connection is the user's money.
     */
    fun enqueue(context: Context, zone: Zone, wifiOnly: Boolean): Long {
        OfflineMaps.baseDir(context).mkdirs()
        partFile(context, zone).delete()

        val request = DownloadManager.Request(Uri.parse(BASE_URL + zone.id + ".map"))
            .setTitle(zone.label)
            .setDescription("Offline map for IMU Logger")
            .setDestinationInExternalFilesDir(context, null, "osmdroid/${zone.id}.map.part")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setAllowedOverMetered(!wifiOnly)
            .setAllowedOverRoaming(false)

        val id = manager(context).enqueue(request)
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putLong(zone.id, id).apply()
        Log.i(TAG, "Queued ${zone.id} as download $id (wifiOnly=$wifiOnly)")
        return id
    }

    fun cancel(context: Context, zone: Zone) {
        val id = downloadId(context, zone) ?: return
        manager(context).remove(id)
        clearDownloadId(context, zone)
        partFile(context, zone).delete()
    }

    fun delete(context: Context, zone: Zone): Boolean = installedFile(context, zone).delete()

    // ------------------------------------------------------------------ progress

    data class Progress(val status: Int, val bytesDone: Long, val bytesTotal: Long) {
        val running: Boolean
            get() = status == DownloadManager.STATUS_RUNNING ||
                status == DownloadManager.STATUS_PENDING ||
                status == DownloadManager.STATUS_PAUSED

        val percent: Int
            get() = if (bytesTotal > 0) ((100 * bytesDone) / bytesTotal).toInt() else 0
    }

    fun progress(context: Context, zone: Zone): Progress? {
        val id = downloadId(context, zone) ?: return null
        manager(context).query(DownloadManager.Query().setFilterById(id)).use { c ->
            if (c == null || !c.moveToFirst()) return null
            val status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
            val done = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR))
            val total = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES))
            return Progress(status, done, total)
        }
    }

    /**
     * Promote any finished download into place.
     *
     * Returns the zones that became usable, so the caller can re-attach the map. Verifying the
     * magic bytes first means a captive-portal HTML page or a truncated transfer is discarded
     * here rather than surfacing later as an unexplained fallback to raster tiles.
     */
    fun promoteCompleted(context: Context): List<Zone> {
        val promoted = mutableListOf<Zone>()
        for (zone in INDIA_ZONES) {
            val id = downloadId(context, zone) ?: continue
            val status = progress(context, zone)?.status ?: continue
            if (status != DownloadManager.STATUS_SUCCESSFUL) {
                if (status == DownloadManager.STATUS_FAILED) {
                    Log.w(TAG, "Download of ${zone.id} failed")
                    clearDownloadId(context, zone)
                    partFile(context, zone).delete()
                }
                continue
            }

            val part = partFile(context, zone)
            if (!part.isFile || !hasMapsforgeHeader(part)) {
                Log.e(TAG, "${part.name} is not a Mapsforge file; discarding")
                part.delete()
                clearDownloadId(context, zone)
                continue
            }
            val target = installedFile(context, zone)
            target.delete()
            if (part.renameTo(target)) {
                promoted.add(zone)
                Log.i(TAG, "Installed ${target.name} (${target.length()} bytes)")
            } else {
                Log.e(TAG, "Could not move ${part.name} into place")
            }
            clearDownloadId(context, zone)
        }
        return promoted
    }

    private fun hasMapsforgeHeader(file: File): Boolean = try {
        file.inputStream().use { input ->
            val head = ByteArray(MAGIC.size)
            input.read(head) == MAGIC.size && head.contentEquals(MAGIC)
        }
    } catch (e: Exception) {
        Log.e(TAG, "Could not read ${file.name}", e)
        false
    }

    // ------------------------------------------------------------------ plumbing

    private fun manager(context: Context): DownloadManager =
        context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager

    private fun downloadId(context: Context, zone: Zone): Long? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getLong(zone.id, -1L)
            .takeIf { it >= 0 }

    private fun clearDownloadId(context: Context, zone: Zone) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().remove(zone.id).apply()
    }
}
