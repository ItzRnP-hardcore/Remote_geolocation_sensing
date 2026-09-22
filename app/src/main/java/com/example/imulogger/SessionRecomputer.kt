package com.example.imulogger

import android.content.Context
import android.hardware.SensorManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.io.File
import java.io.FileInputStream
import java.io.FileReader
import java.io.FileWriter
import java.io.InputStreamReader
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * On-device re-simulation engine that takes raw recorded IMU and GPS streams from past sessions
 * and recomputes the dead-reckoned and map-matched tracks using the latest kinematic algorithms
 * (Centripetal compensation, Dual ZUPT, and Adaptive speed gain) alongside RoadNetwork MapMatcher.
 */
object SessionRecomputer {

    data class RecomputeResult(
        val recomputedTrack: List<TrackPoint>,
        val mapMatchedTrack: List<TrackPoint>,
        val originalDriftM: Double,
        val newDriftM: Double,
        val mapMatchedDriftM: Double,
        val improvementPct: Double,
        val totalDistanceM: Double,
    )

    suspend fun recomputeSession(
        context: Context,
        sessionDir: File,
        onProgress: (Int) -> Unit = {},
    ): RecomputeResult? = withContext(Dispatchers.Default) {
        val gpsFile = File(sessionDir, "gps.csv")
        val imuFile = File(sessionDir, "imu.csv")
        val origDrFile = File(sessionDir, "deadreckon.csv")

        if (!gpsFile.exists() || !imuFile.exists()) return@withContext null

        // 1. Parse GPS track to seed origin and measure ground truth drift
        val gpsPoints = mutableListOf<TrackPoint>()
        var startLat = Double.NaN
        var startLon = Double.NaN
        var startSpeed = 0f
        var startBearing = 0f
        var hasStartFix = false

        BufferedReader(FileReader(gpsFile)).use { reader ->
            val header = reader.readLine()?.split(",") ?: return@withContext null
            val latIdx = header.indexOfFirst { it.equals("lat", true) || it.equals("latitude", true) }
            val lonIdx = header.indexOfFirst { it.equals("lon", true) || it.equals("longitude", true) }
            val spdIdx = header.indexOfFirst { it.equals("speed_mps", true) || it.equals("speed", true) }
            val brgIdx = header.indexOfFirst { it.equals("bearing_deg", true) || it.equals("bearing", true) }

            if (latIdx == -1 || lonIdx == -1) return@withContext null

            var line: String?
            while (reader.readLine().also { line = it } != null) {
                val parts = line!!.split(",")
                if (parts.size <= maxOf(latIdx, lonIdx)) continue
                val lat = parts[latIdx].toDoubleOrNull() ?: continue
                val lon = parts[lonIdx].toDoubleOrNull() ?: continue
                val spd = if (spdIdx != -1) parts.getOrNull(spdIdx)?.toFloatOrNull() ?: 0f else 0f
                val brg = if (brgIdx != -1) parts.getOrNull(brgIdx)?.toFloatOrNull() ?: 0f else 0f

                gpsPoints.add(TrackPoint(lat, lon))
                if (!hasStartFix && !lat.isNaN() && !lon.isNaN()) {
                    startLat = lat
                    startLon = lon
                    startSpeed = spd
                    startBearing = brg
                    hasStartFix = true
                }
            }
        }

        if (!hasStartFix || gpsPoints.isEmpty()) return@withContext null
        val lastGps = gpsPoints.last()

        // 2. Compute original drift from old deadreckon.csv if available
        var origDriftM = Double.NaN
        if (origDrFile.exists()) {
            val oldDrPoints = mutableListOf<TrackPoint>()
            BufferedReader(FileReader(origDrFile)).use { reader ->
                reader.readLine()
                var line: String?
                while (reader.readLine().also { line = it } != null) {
                    val p = line!!.split(",")
                    if (p.size >= 3) {
                        val lat = p[1].toDoubleOrNull() ?: continue
                        val lon = p[2].toDoubleOrNull() ?: continue
                        oldDrPoints.add(TrackPoint(lat, lon))
                    }
                }
            }
            if (oldDrPoints.isNotEmpty()) {
                val lastOldDr = oldDrPoints.last()
                origDriftM = haversine(lastOldDr.lat, lastOldDr.lon, lastGps.lat, lastGps.lon)
            }
        }

        // 3. Initialize RoadNetwork and MapMatcher from offline map asset
        val mapFiles = MapsforgeSource.mapFiles(context)
        val roadNetwork = mapFiles.firstOrNull()?.let { file ->
            val net = RoadNetwork(file)
            if (net.open()) net else null
        }
        val mapMatcher = if (roadNetwork != null) MapMatcher(roadNetwork) else null

        // 4. Initialize DeadReckoner with Vehicle NHC and TCN model runner
        val dr = DeadReckoner().apply {
            isPhoneFixed = true
            anchorTo(startLat, startLon, startSpeed, startBearing)
        }

        val modelRunner = try {
            IMUModelRunner(context)
        } catch (_: Exception) {
            null
        }

        val recomputedTrack = mutableListOf<TrackPoint>()
        val mapMatchedTrack = mutableListOf<TrackPoint>()

        dr.position?.let {
            recomputedTrack.add(it)
            val startMatch = mapMatcher?.update(it.lat, it.lon, startBearing.toDouble(), startSpeed.toDouble(), 0.0, forceSnap = true)
            if (startMatch != null) {
                mapMatchedTrack.add(TrackPoint(startMatch.lat, startMatch.lon))
            } else {
                mapMatchedTrack.add(it)
            }
        }

        val rot = FloatArray(9)
        var lastGx = 0f
        var lastGy = 0f
        var lastGz = 0f
        var lastGyroNs = 0L
        var lastModelNs = 0L
        var lastRecordNs = 0L

        val totalBytes = imuFile.length().coerceAtLeast(1L)
        var bytesRead = 0L
        var lastProgressPct = -1

        // 5. Stream and process IMU events through DeadReckoner, Gyro-AHRS & MapMatcher
        try {
            FileInputStream(imuFile).use { fis ->
                BufferedReader(InputStreamReader(fis)).use { reader ->
                    reader.readLine() // skip header
                    var line: String?
                    while (reader.readLine().also { line = it } != null) {
                        val curLine = line!!
                        bytesRead += curLine.length + 1
                        val pct = ((bytesRead * 100) / totalBytes).toInt().coerceIn(0, 100)
                        if (pct != lastProgressPct && pct % 5 == 0) {
                            lastProgressPct = pct
                            onProgress(pct)
                        }

                        val parts = curLine.split(",")
                        if (parts.size < 6) continue
                        val tNs = parts[0].toLongOrNull() ?: continue
                        val sensor = parts[1]
                        val v0 = parts[3].toFloatOrNull() ?: continue
                        val v1 = parts[4].toFloatOrNull() ?: continue
                        val v2 = parts[5].toFloatOrNull() ?: continue

                        when (sensor) {
                            "rv", "game_rv" -> {
                                val v3 = parts.getOrNull(6)?.toFloatOrNull() ?: 0f
                                dr.onRotationVector(floatArrayOf(v0, v1, v2, v3))
                            }
                            "gyro" -> {
                                lastGx = v0
                                lastGy = v1
                                lastGz = v2
                                dr.onGyro(v0, v1, v2)

                                // Steer heading using true physical gyro in world frame (matches live SensorService)
                                val prevG = lastGyroNs
                                lastGyroNs = tNs
                                if (prevG != 0L && dr.rotationInto(rot)) {
                                    val dtG = ((tNs - prevG) / 1e9).coerceIn(0.001, 0.05)
                                    val gb = dr.gyroBias
                                    val gx = v0 - gb[0].toFloat()
                                    val gy = v1 - gb[1].toFloat()
                                    val gz = v2 - gb[2].toFloat()
                                    // Project gyro onto world vertical axis
                                    val wU = rot[6] * gx + rot[7] * gy + rot[8] * gz
                                    val yawCw = -wU.toDouble()
                                    dr.onYawRate(yawCw, dtG)
                                }
                            }
                            "accel" -> {
                                dr.onAccel(tNs, v0, v1, v2)

                                // Downsample to 10 Hz for neural network sliding window inference
                                if (tNs - lastModelNs >= 100_000_000L && dr.rotationInto(rot)) {
                                    lastModelNs = tNs
                                    val eax = rot[0] * v0 + rot[1] * v1 + rot[2] * v2
                                    val eay = rot[3] * v0 + rot[4] * v1 + rot[5] * v2
                                    val eaz = rot[6] * v0 + rot[7] * v1 + rot[8] * v2

                                    val out = modelRunner?.processIMUData(
                                        eax, eay, eaz - 9.80665f,
                                        lastGx, lastGy, lastGz,
                                    )

                                    if (out != null) {
                                        val statLogit = out[IMUModelRunner.IDX_STATIONARY]
                                        val isStat = statLogit >= IMUModelRunner.STATIONARY_LOGIT_THRESHOLD
                                        // Stationarity logit for Dual ZUPT clamping: correctly pass true or false!
                                        dr.onStationarySignal(isStat)

                                        // Apply model speed when moving to advance dead reckoner at vehicle speed
                                        if (!isStat) {
                                            val mu = out[IMUModelRunner.IDX_MU]
                                            val logvar = out[IMUModelRunner.IDX_LOGVAR]
                                            val weight = IMUModelRunner.fusionWeight(logvar)
                                            dr.applyModelSpeed(mu.toDouble(), weight)
                                        }
                                    }
                                }

                                // Record track point every 0.5s for display and map matching
                                if (tNs - lastRecordNs >= 500_000_000L) {
                                    lastRecordNs = tNs
                                    dr.position?.let { p ->
                                        recomputedTrack.add(p)

                                        // Run MapMatcher HMM snap
                                        val course = dr.courseDeg
                                        val speed = dr.speed
                                        val uncertainty = dr.positionSigmaM
                                        val m = mapMatcher?.update(p.lat, p.lon, course, speed, uncertainty, forceSnap = true)
                                        if (m != null) {
                                            mapMatchedTrack.add(TrackPoint(m.lat, m.lon))
                                            // Close the loop: feed road bearing back to DeadReckoner heading
                                            if (m.confidence >= MapMatcher.HEADING_FEEDBACK_MIN_CONFIDENCE && m.correctionM <= 25.0) {
                                                dr.applyHeadingCorrection(m.roadBearingDeg, MapMatcher.HEADING_FEEDBACK_GAIN)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } finally {
            roadNetwork?.close()
        }

        dr.position?.let {
            recomputedTrack.add(it)
        }
        onProgress(100)

        // 6. Evaluate drift results
        val finalDr = recomputedTrack.last()
        val newDriftM = haversine(finalDr.lat, finalDr.lon, lastGps.lat, lastGps.lon)

        val finalMm = mapMatchedTrack.lastOrNull() ?: finalDr
        val mapMatchedDriftM = if (mapMatchedTrack.isNotEmpty()) {
            haversine(finalMm.lat, finalMm.lon, lastGps.lat, lastGps.lon)
        } else Double.NaN

        var totalDistM = 0.0
        for (i in 0 until gpsPoints.size - 1) {
            totalDistM += haversine(gpsPoints[i].lat, gpsPoints[i].lon, gpsPoints[i + 1].lat, gpsPoints[i + 1].lon)
        }

        val improvementPct = if (!origDriftM.isNaN() && origDriftM > 0.0) {
            ((origDriftM - newDriftM) / origDriftM) * 100.0
        } else 0.0

        // Persist recomputed files to session folder
        try {
            val recomputedFile = File(sessionDir, "deadreckon_recomputed.csv")
            FileWriter(recomputedFile).use { writer ->
                writer.write("index,lat,lon\n")
                recomputedTrack.forEachIndexed { idx, pt ->
                    writer.write("$idx,${pt.lat},${pt.lon}\n")
                }
            }

            val mapMatchRecomputedFile = File(sessionDir, "mapmatch_recomputed.csv")
            FileWriter(mapMatchRecomputedFile).use { writer ->
                writer.write("index,lat,lon\n")
                mapMatchedTrack.forEachIndexed { idx, pt ->
                    writer.write("$idx,${pt.lat},${pt.lon}\n")
                }
            }
        } catch (_: Exception) {}

        RecomputeResult(
            recomputedTrack = recomputedTrack,
            mapMatchedTrack = mapMatchedTrack,
            originalDriftM = if (origDriftM.isNaN()) newDriftM else origDriftM,
            newDriftM = newDriftM,
            mapMatchedDriftM = mapMatchedDriftM,
            improvementPct = improvementPct,
            totalDistanceM = totalDistM,
        )
    }

    private fun haversine(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6371000.0
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = sin(dLat / 2) * sin(dLat / 2) +
                cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) *
                sin(dLon / 2) * sin(dLon / 2)
        val c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return r * c
    }
}
