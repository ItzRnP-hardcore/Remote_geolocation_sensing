package com.example.imulogger

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.util.Log
import java.io.File
import java.io.FileInputStream
import java.io.InputStream
import java.util.Collections

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
 *
 * Promotion is not automatic on DownloadManager's side: it writes the file and sends one
 * broadcast, and that is all. Somebody has to call [promoteCompleted] afterwards, and because the
 * user will almost always have left the app by the time a 200 MB transfer lands, that call has to
 * come from the activity lifecycle and the completion broadcast, not from a dialog that happens
 * to be open.
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

    /**
     * Zones whose download has finished and whose file is being verified and moved into place.
     * The copy fallback in [promoteCompleted] can take several seconds for a half-gigabyte file,
     * and the settings sheet uses this to say "Installing" rather than offering Download again.
     */
    val installing: MutableSet<String> = Collections.synchronizedSet(mutableSetOf<String>())

    /** True when at least one tracked download has left the running state and needs promoting. */
    fun hasCompletedDownloads(context: Context): Boolean = INDIA_ZONES.any { zone ->
        downloadId(context, zone) != null && progress(context, zone)?.running == false
    }

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
     * Promote any finished download into place. Does file I/O, so call it off the main thread.
     *
     * Returns the zones that became usable, so the caller can re-attach the map. Verifying the
     * magic bytes first means a captive-portal HTML page or a truncated transfer is discarded
     * here rather than surfacing later as an unexplained fallback to raster tiles.
     *
     * The finished file belongs to the system download provider, not to this app, so a plain
     * rename can be refused even though it sits in our own external-files directory. The header
     * is therefore read through [DownloadManager.openDownloadedFile], which the provider grants
     * regardless of ownership, and if the rename is refused the same descriptor is copied into
     * place instead. Either way the provider's record is removed afterwards so the `.part` file
     * does not linger in the Downloads app.
     */
    fun promoteCompleted(context: Context): List<Zone> {
        val promoted = mutableListOf<Zone>()
        for (zone in INDIA_ZONES) {
            val id = downloadId(context, zone) ?: continue
            val status = progress(context, zone)?.status
            if (status == null) {
                // The provider no longer knows this id: the user cleared it from the Downloads
                // app, or the record was reaped. Nothing to promote; stop tracking it.
                clearDownloadId(context, zone)
                partFile(context, zone).delete()
                continue
            }
            if (status != DownloadManager.STATUS_SUCCESSFUL) {
                if (status == DownloadManager.STATUS_FAILED) {
                    Log.w(TAG, "Download of ${zone.id} failed")
                    manager(context).remove(id)
                    clearDownloadId(context, zone)
                    partFile(context, zone).delete()
                }
                continue
            }

            installing.add(zone.id)
            try {
                if (install(context, zone, id)) promoted.add(zone)
            } finally {
                installing.remove(zone.id)
            }
        }
        return promoted
    }

    private fun install(context: Context, zone: Zone, id: Long): Boolean {
        val manager = manager(context)
        val target = installedFile(context, zone)
        val part = downloadedFile(context, zone, id)

        val valid = try {
            manager.openDownloadedFile(id).use { pfd ->
                FileInputStream(pfd.fileDescriptor).use { hasMapsforgeHeader(it) }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Cannot open finished download ${zone.id}", e)
            false
        }
        if (!valid) {
            Log.e(TAG, "Download of ${zone.id} is not a Mapsforge file; discarding")
            manager.remove(id)
            clearDownloadId(context, zone)
            part.delete()
            return false
        }

        target.delete()
        var installed = part.isFile && part.renameTo(target)
        if (!installed) {
            // Rename refused, or the provider put the file somewhere we did not expect. Copy the
            // bytes through the provider's descriptor into a temp file and rename that instead, so
            // a kill mid-copy can never leave a truncated `.map` behind.
            Log.i(TAG, "Rename of ${part.name} refused; copying ${zone.id} into place")
            val tmp = File(target.parentFile, "${zone.id}.map.tmp")
            installed = try {
                manager.openDownloadedFile(id).use { pfd ->
                    FileInputStream(pfd.fileDescriptor).use { input ->
                        tmp.outputStream().use { output ->
                            input.copyTo(output, 1 shl 16)
                            output.fd.sync()
                        }
                    }
                }
                tmp.renameTo(target)
            } catch (e: Exception) {
                Log.e(TAG, "Copy of ${zone.id} failed", e)
                false
            }
            if (!installed) tmp.delete()
        }

        if (installed) {
            Log.i(TAG, "Installed ${target.name} (${target.length()} bytes)")
            // Drop the provider's record. The bytes are ours now; if the provider still holds
            // its own copy this deletes it, and if we renamed it away this just tidies the list.
            manager.remove(id)
            clearDownloadId(context, zone)
            part.delete()
        } else {
            Log.e(TAG, "Could not move ${zone.id} into place; will retry next time")
        }
        return installed
    }

    /**
     * Where the provider actually wrote the file. Normally the `.part` path we asked for, but
     * DownloadManager renames on collision (`name-1.map.part`), and its own record is the truth.
     */
    private fun downloadedFile(context: Context, zone: Zone, id: Long): File {
        val expected = partFile(context, zone)
        return try {
            manager(context).query(DownloadManager.Query().setFilterById(id)).use { c ->
                if (c == null || !c.moveToFirst()) return expected
                val uri = c.getString(c.getColumnIndexOrThrow(DownloadManager.COLUMN_LOCAL_URI))
                    ?: return expected
                Uri.parse(uri).path?.let { File(it) }?.takeIf { it.isFile } ?: expected
            }
        } catch (e: Exception) {
            expected
        }
    }

    /** True when [file] starts with the Mapsforge magic, so it is at least the right kind of file. */
    fun hasMapsforgeHeader(file: File): Boolean = try {
        file.inputStream().use { hasMapsforgeHeader(it) }
    } catch (e: Exception) {
        Log.e(TAG, "Could not read ${file.name}", e)
        false
    }

    private fun hasMapsforgeHeader(input: InputStream): Boolean {
        val head = ByteArray(MAGIC.size)
        var read = 0
        while (read < head.size) {
            val n = input.read(head, read, head.size - read)
            if (n < 0) break
            read += n
        }
        return read == MAGIC.size && head.contentEquals(MAGIC)
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
