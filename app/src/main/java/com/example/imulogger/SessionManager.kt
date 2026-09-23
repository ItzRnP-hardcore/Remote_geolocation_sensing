package com.example.imulogger

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.osmdroid.util.BoundingBox
import java.io.BufferedReader
import java.io.File
import java.io.FileReader
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Discovers and parses recorded sessions for map visualization and track comparison.
 */
object SessionManager {

    data class SessionSummary(
        val id: String,
        val dir: File,
        val formattedDate: String,
        val durationSeconds: Double,
        val imuSamples: Long,
        val gpsFixes: Long,
        val hasSnap: Boolean,
    )

    data class LoadedSession(
        val summary: SessionSummary,
        val gpsTrack: List<TrackPoint>,
        val drTrack: List<TrackPoint>,
        val snapTrack: List<TrackPoint>,
        val bounds: BoundingBox?,
    )

    /**
     * Finds all session folders on the device, sorted newest first.
     */
    suspend fun listSessions(context: Context): List<SessionSummary> = withContext(Dispatchers.IO) {
        val dirs = mutableListOf<File>()
        context.getExternalFilesDir(null)?.let { File(it, "sessions").takeIf { f -> f.isDirectory }?.let { dirs.add(it) } }
        File(context.filesDir, "sessions").takeIf { it.isDirectory }?.let { dirs.add(it) }

        val sessions = mutableListOf<SessionSummary>()
        val seen = mutableSetOf<String>()

        for (parent in dirs) {
            val children = parent.listFiles { f -> f.isDirectory } ?: continue
            for (dir in children) {
                if (!seen.add(dir.name)) continue
                val summary = readSummary(dir) ?: continue
                sessions.add(summary)
            }
        }

        sessions.sortedByDescending { it.id }
    }

    private fun readSummary(dir: File): SessionSummary? {
        val id = dir.name
        val sessionJson = File(dir, "session.json")
        val gpsFile = File(dir, "gps.csv")
        val drFile = File(dir, "deadreckon.csv")
        if (!sessionJson.exists() && !gpsFile.exists() && !drFile.exists()) return null

        var durationS = 0.0
        var imuCount = 0L
        var gpsCount = 0L

        if (sessionJson.exists()) {
            try {
                val json = JSONObject(sessionJson.readText())
                val summary = json.optJSONObject("summary")
                if (summary != null) {
                    durationS = summary.optDouble("duration_s", 0.0)
                    imuCount = summary.optLong("imu_samples", 0L)
                    gpsCount = summary.optLong("gps_fixes", 0L)
                }
            } catch (_: Exception) {}
        }

        // Format date from ID (e.g. 20260904_195146)
        val formatted = try {
            val parser = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
            val d = parser.parse(id)
            if (d != null) {
                SimpleDateFormat("MMM d, yyyy · HH:mm", Locale.US).format(d)
            } else id
        } catch (_: Exception) {
            id
        }

        val hasSnap = File(dir, "mapmatch.csv").exists()

        return SessionSummary(
            id = id,
            dir = dir,
            formattedDate = formatted,
            durationSeconds = durationS,
            imuSamples = imuCount,
            gpsFixes = gpsCount,
            hasSnap = hasSnap,
        )
    }

    /**
     * Reads and parses track files for the session in a background thread.
     */
    suspend fun loadSession(summary: SessionSummary): LoadedSession = withContext(Dispatchers.IO) {
        val gpsPoints = parseGpsCsv(File(summary.dir, "gps.csv"))
        val recomputedDr = File(summary.dir, "deadreckon_recomputed.csv")
        val drFile = if (recomputedDr.exists() && recomputedDr.length() > 50) recomputedDr else File(summary.dir, "deadreckon.csv")
        val drPoints = parseDrCsv(drFile)

        val recomputedSnap = File(summary.dir, "mapmatch_recomputed.csv")
        val snapFile = if (recomputedSnap.exists() && recomputedSnap.length() > 50) recomputedSnap else File(summary.dir, "mapmatch.csv")
        val snapPoints = parseSnapCsv(snapFile)

        var minLat = 90.0
        var maxLat = -90.0
        var minLon = 180.0
        var maxLon = -180.0
        var count = 0

        fun expand(p: TrackPoint) {
            if (p.lat.isNaN() || p.lon.isNaN()) return
            if (p.lat < minLat) minLat = p.lat
            if (p.lat > maxLat) maxLat = p.lat
            if (p.lon < minLon) minLon = p.lon
            if (p.lon > maxLon) maxLon = p.lon
            count++
        }

        gpsPoints.forEach { expand(it) }
        drPoints.forEach { expand(it) }
        snapPoints.forEach { expand(it) }

        val bounds = if (count > 0 && maxLat >= minLat && maxLon >= minLon) {
            // Add a 5% margin around the box
            val latPad = ((maxLat - minLat) * 0.05).coerceAtLeast(0.001)
            val lonPad = ((maxLon - minLon) * 0.05).coerceAtLeast(0.001)
            BoundingBox(
                (maxLat + latPad).coerceAtMost(85.0),
                (maxLon + lonPad).coerceAtMost(180.0),
                (minLat - latPad).coerceAtLeast(-85.0),
                (minLon - lonPad).coerceAtLeast(-180.0),
            )
        } else null

        LoadedSession(
            summary = summary,
            gpsTrack = gpsPoints,
            drTrack = drPoints,
            snapTrack = snapPoints,
            bounds = bounds,
        )
    }

    private fun parseGpsCsv(file: File): List<TrackPoint> {
        if (!file.exists()) return emptyList()
        val list = mutableListOf<TrackPoint>()
        try {
            BufferedReader(FileReader(file)).use { reader ->
                val headerLine = reader.readLine() ?: return emptyList()
                val headers = headerLine.split(',').map { it.trim() }
                val latIdx = headers.indexOf("lat")
                val lonIdx = headers.indexOf("lon")
                val accIdx = headers.indexOf("acc_m")
                if (latIdx < 0 || lonIdx < 0) return emptyList()

                var line = reader.readLine()
                while (line != null) {
                    val parts = line.split(',')
                    if (parts.size > maxOf(latIdx, lonIdx)) {
                        val lat = parts[latIdx].toDoubleOrNull()
                        val lon = parts[lonIdx].toDoubleOrNull()
                        val acc = if (accIdx in parts.indices) parts[accIdx].toFloatOrNull() else null
                        if (lat != null && lon != null) {
                            val quality = if (acc != null && acc > 20f) GnssQuality.WEAK else GnssQuality.GOOD
                            list.add(TrackPoint(lat, lon, quality))
                        }
                    }
                    line = reader.readLine()
                }
            }
        } catch (_: Exception) {}
        return list
    }

    private fun parseDrCsv(file: File): List<TrackPoint> {
        if (!file.exists()) return emptyList()
        val list = mutableListOf<TrackPoint>()
        try {
            BufferedReader(FileReader(file)).use { reader ->
                val headerLine = reader.readLine() ?: return emptyList()
                val headers = headerLine.split(',').map { it.trim() }
                val latIdx = headers.indexOf("lat")
                val lonIdx = headers.indexOf("lon")
                val freeRunIdx = headers.indexOf("free_run")
                if (latIdx < 0 || lonIdx < 0) return emptyList()

                var line = reader.readLine()
                var counter = 0
                while (line != null) {
                    counter++
                    // DR records at 10 Hz; if trace is very long, sample every 2nd point to keep UI fast
                    val parts = line.split(',')
                    if (parts.size > maxOf(latIdx, lonIdx)) {
                        val lat = parts[latIdx].toDoubleOrNull()
                        val lon = parts[lonIdx].toDoubleOrNull()
                        val freeRun = if (freeRunIdx in parts.indices) parts[freeRunIdx].toIntOrNull() == 1 else false
                        if (lat != null && lon != null) {
                            val quality = if (freeRun) GnssQuality.LOST else GnssQuality.GOOD
                            list.add(TrackPoint(lat, lon, quality))
                        }
                    }
                    line = reader.readLine()
                }
            }
        } catch (_: Exception) {}
        return list
    }

    private fun parseSnapCsv(file: File): List<TrackPoint> {
        if (!file.exists()) return emptyList()
        val list = mutableListOf<TrackPoint>()
        try {
            BufferedReader(FileReader(file)).use { reader ->
                val headerLine = reader.readLine() ?: return emptyList()
                val headers = headerLine.split(',').map { it.trim() }
                var latIdx = headers.indexOf("snap_lat")
                var lonIdx = headers.indexOf("snap_lon")
                if (latIdx < 0 || lonIdx < 0) {
                    latIdx = headers.indexOf("lat")
                    lonIdx = headers.indexOf("lon")
                }
                if (latIdx < 0 || lonIdx < 0) return emptyList()

                var line = reader.readLine()
                while (line != null) {
                    val parts = line.split(',')
                    if (parts.size > maxOf(latIdx, lonIdx)) {
                        val lat = parts[latIdx].toDoubleOrNull()
                        val lon = parts[lonIdx].toDoubleOrNull()
                        if (lat != null && lon != null) {
                            list.add(TrackPoint(lat, lon, GnssQuality.GOOD))
                        }
                    }
                    line = reader.readLine()
                }
            }
        } catch (_: Exception) {}
        return list
    }
}
