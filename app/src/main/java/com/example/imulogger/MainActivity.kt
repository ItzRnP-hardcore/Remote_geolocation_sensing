package com.example.imulogger

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.example.imulogger.databinding.ActivityMainBinding
import kotlinx.coroutines.launch
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.overlay.Marker
import org.osmdroid.views.overlay.Polyline
import org.osmdroid.views.overlay.mylocation.MyLocationNewOverlay
import org.osmdroid.views.overlay.mylocation.GpsMyLocationProvider
import android.graphics.DashPathEffect
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var trackLine: Polyline
    private lateinit var drLine: Polyline
    private lateinit var marker: Marker
    private lateinit var drMarker: Marker
    private lateinit var locationOverlay: MyLocationNewOverlay

    private var offline = true
    private var followPosition = true
    private var trackSize = 0
    private var drSize = 0

    private var radiusKm = 10.0
    private var lastPrefetchCentre: GeoPoint? = null
    private var prefetching = false

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        if (granted[Manifest.permission.ACCESS_FINE_LOCATION] == true) {
            startRecording()
        } else {
            Toast.makeText(
                this,
                "Precise location is required: the service records GNSS fixes and cannot run " +
                    "as a location foreground service without it.",
                Toast.LENGTH_LONG,
            ).show()
        }
        render(SensorService.status.value)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Must precede inflation — MapView reads osmdroid's configuration as it is constructed.
        OfflineMaps.configure(this)

        copyMapFromAssetsIfNeeded()

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setUpMap()

        binding.btnRecord.setOnClickListener {
            if (SensorService.status.value.running) {
                stopService(Intent(this, SensorService::class.java))
            } else if (hasFineLocation()) {
                startRecording()
            } else {
                requestPermissions()
            }
        }

        binding.chipOffline.setOnClickListener {
            offline = !offline
            OfflineMaps.apply(binding.map, offline)
            binding.map.invalidate()
            renderTileState()
        }

        binding.chipPreload.setOnClickListener { askPrefetchRadius() }

        binding.btnFreeRun.setOnClickListener {
            SensorService.setFreeRun(!SensorService.status.value.freeRun)
        }

        binding.fabCentre.setOnClickListener {
            followPosition = true
            if (::locationOverlay.isInitialized) {
                locationOverlay.enableFollowLocation()
                val myLoc = locationOverlay.myLocation
                if (myLoc != null) {
                    binding.map.controller.animateTo(myLoc)
                    return@setOnClickListener
                }
            }
            SensorService.status.value.let { s ->
                if (s.lastLat != null && s.lastLon != null) {
                    binding.map.controller.animateTo(GeoPoint(s.lastLat, s.lastLon))
                }
            }
        }

        // The UI follows the service, not the last tap: if recording stops on its own, the
        // button and chips correct themselves.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch { SensorService.status.collect { render(it) } }
                launch { SensorService.track.collect { drawTrack(it) } }
                launch { SensorService.drTrack.collect { drawDrTrack(it) } }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        binding.map.onResume()
    }

    override fun onPause() {
        binding.map.onPause()
        super.onPause()
    }

    // ------------------------------------------------------------------ map

    private fun setUpMap() = with(binding.map) {
        val vectorBounds = OfflineMaps.apply(this, offline)
        setMultiTouchControls(true)
        isVerticalMapRepetitionEnabled = false
        isHorizontalMapRepetitionEnabled = false
        // Remove isTilesScaledToDpi as it scales bitmaps, causing blurriness and making text too large/wrapped
        controller.setZoom(16.0)
        // Any manual pan means the user wants to look somewhere else; stop yanking the camera back.
        setOnTouchListener { v, _ -> 
            followPosition = false
            if (::locationOverlay.isInitialized) locationOverlay.disableFollowLocation()
            v.performClick()
            false 
        }

        locationOverlay = MyLocationNewOverlay(GpsMyLocationProvider(context), this)
        
        // Custom blue dot for location
        val size = (16 * resources.displayMetrics.density).toInt()
        val blueDot = android.graphics.Bitmap.createBitmap(size, size, android.graphics.Bitmap.Config.ARGB_8888)
        val canvas = android.graphics.Canvas(blueDot)
        val paint = android.graphics.Paint().apply {
            color = android.graphics.Color.parseColor("#4285F4")
            style = android.graphics.Paint.Style.FILL
            isAntiAlias = true
        }
        val center = size / 2f
        canvas.drawCircle(center, center, center, paint)
        val borderPaint = android.graphics.Paint().apply {
            color = android.graphics.Color.WHITE
            style = android.graphics.Paint.Style.STROKE
            strokeWidth = 2f * resources.displayMetrics.density
            isAntiAlias = true
        }
        canvas.drawCircle(center, center, center - borderPaint.strokeWidth / 2, borderPaint)
        
        locationOverlay.setPersonIcon(blueDot)
        locationOverlay.setPersonHotspot(center, center)
        
        // Custom arrow for directional location
        val arrowBitmap = android.graphics.Bitmap.createBitmap(size, size, android.graphics.Bitmap.Config.ARGB_8888)
        val arrowCanvas = android.graphics.Canvas(arrowBitmap)
        val arrowPath = android.graphics.Path().apply {
            moveTo(size / 2f, 0f) // Top tip (North)
            lineTo(size.toFloat(), size.toFloat()) // Bottom right
            lineTo(size / 2f, size * 0.75f) // Bottom center indent
            lineTo(0f, size.toFloat()) // Bottom left
            close()
        }
        arrowCanvas.drawPath(arrowPath, paint)
        arrowCanvas.drawPath(arrowPath, borderPaint)
        
        try {
            locationOverlay.setDirectionIcon(arrowBitmap)
            locationOverlay.setPersonHotspot(center, center)
            locationOverlay.setDirectionAnchor(center, center)
        } catch (e: Exception) {
            // Method might be deprecated or unsupported in some versions, fallback to setDirectionArrow if needed
            try {
                // Suppressing deprecation because it's the fallback
                @Suppress("DEPRECATION")
                locationOverlay.setDirectionArrow(blueDot, arrowBitmap)
            } catch (e2: Exception) {
                // Ignore
            }
        }
        
        locationOverlay.enableMyLocation()
        overlays.add(locationOverlay)

        // Dark mode mapping with enhanced road visibility
        val isDark = (resources.configuration.uiMode and android.content.res.Configuration.UI_MODE_NIGHT_MASK) == android.content.res.Configuration.UI_MODE_NIGHT_YES
        if (isDark) {
            val nightMatrix = android.graphics.ColorMatrix(
                floatArrayOf(
                    -1f, 0f, 0f, 0f, 255f,
                    0f, -1f, 0f, 0f, 255f,
                    0f, 0f, -1f, 0f, 255f,
                    0f, 0f, 0f, 1f, 0f
                )
            )
            val contrastMatrix = android.graphics.ColorMatrix(
                floatArrayOf(
                    1.4f, 0f, 0f, 0f, 20f,
                    0f, 1.4f, 0f, 0f, 30f,
                    0f, 0f, 1.6f, 0f, 50f,
                    0f, 0f, 0f, 1f, 0f
                )
            )
            nightMatrix.postConcat(contrastMatrix)
            overlayManager.tilesOverlay.setColorFilter(android.graphics.ColorMatrixColorFilter(nightMatrix))
        }

        trackLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track)
            outlinePaint.strokeWidth = 8f
        }
        // Dashed, so the two tracks stay distinguishable where they overlap and for anyone who
        // cannot separate the blue from the orange.
        drLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track_imu)
            outlinePaint.strokeWidth = 7f
            outlinePaint.pathEffect = DashPathEffect(floatArrayOf(18f, 12f), 0f)
        }
        marker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position)
        }
        drMarker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position_imu)
        }
        overlays.add(trackLine)
        overlays.add(drLine)
        overlays.add(drMarker)
        overlays.add(marker)
        // With no fix yet, framing the data we actually have beats staring at null island.
        vectorBounds?.let { post { zoomToBoundingBox(it, false) } }
        renderTileState()
    }

    private fun drawTrack(points: List<TrackPoint>) {
        if (points.size == trackSize) return
        trackSize = points.size
        trackLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        points.lastOrNull()?.let {
            val here = GeoPoint(it.lat, it.lon)
            marker.position = here
            if (followPosition) binding.map.controller.animateTo(here)
        }
        binding.map.invalidate()
    }

    private fun drawDrTrack(points: List<TrackPoint>) {
        if (points.size == drSize) return
        drSize = points.size
        drLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        points.lastOrNull()?.let { drMarker.position = GeoPoint(it.lat, it.lon) }
        binding.map.invalidate()
    }

    private fun askPrefetchRadius() {
        // Preloading only exists for sources whose terms permit it, so route there first rather
        // than letting the user pick a radius and then be refused.
        val source = binding.map.tileProvider.tileSource
        if (source !is org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase ||
            !TileSources.acceptsBulkDownload(source)
        ) {
            promptForTileSource()
            return
        }

        val status = SensorService.status.value
        val centre = when {
            status.lastLat != null && status.lastLon != null ->
                GeoPoint(status.lastLat, status.lastLon)
            else -> binding.map.mapCenter as? GeoPoint
        }
        if (centre == null) {
            Toast.makeText(this, "No position yet — pan the map to the area first.", Toast.LENGTH_LONG).show()
            return
        }
        if (prefetching) {
            Toast.makeText(this, "Already caching tiles.", Toast.LENGTH_SHORT).show()
            return
        }

        val options = doubleArrayOf(2.0, 5.0, 10.0)
        val labels = options.map { km ->
            val n = TilePrefetcher.estimateTiles(binding.map, centre, km)
            if (n >= 0) "${km.toInt()} km — about $n tiles" else "${km.toInt()} km"
        }.toTypedArray()

        MaterialAlertDialogBuilder(this)
            .setTitle("Cache tiles around this point")
            .setItems(labels) { _, which -> startPrefetch(centre, options[which]) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /**
     * OpenStreetMap's public servers forbid bulk download and osmdroid enforces it, so preloading
     * needs a source the user is entitled to bulk-fetch from. Rather than shipping someone else's
     * API key, ask for a template.
     */
    private fun promptForTileSource() {
        val input = android.widget.EditText(this).apply {
            hint = "https://example.com/tiles/{z}/{x}/{y}.png?apikey=..."
            setText(TileSources.template(this@MainActivity).orEmpty())
            setSingleLine()
        }
        val pad = (20 * resources.displayMetrics.density).toInt()
        val wrap = android.widget.FrameLayout(this).apply {
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
        }

        MaterialAlertDialogBuilder(this)
            .setTitle("Tile source for preloading")
            .setMessage(
                "OpenStreetMap's public tiles render fine but cannot be bulk-downloaded — their " +
                    "usage policy forbids it and osmdroid enforces that." +
                    System.lineSeparator() + System.lineSeparator() +
                    "To preload a radius, paste a {z}/{x}/{y} tile URL from a provider whose terms " +
                    "allow caching. Thunderforest and Stadia Maps both issue free keys without a " +
                    "payment card." +
                    System.lineSeparator() + System.lineSeparator() +
                    "Alternatively, drop an .mbtiles archive into the app folder and skip " +
                    "downloading entirely."
            )
            .setView(wrap)
            .setPositiveButton("Save") { _, _ ->
                TileSources.setTemplate(this, input.text.toString())
                OfflineMaps.apply(binding.map, offline)
                binding.map.invalidate()
                renderTileState()
            }
            .setNeutralButton("Clear") { _, _ ->
                TileSources.setTemplate(this, null)
                OfflineMaps.apply(binding.map, offline)
                binding.map.invalidate()
                renderTileState()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun startPrefetch(centre: GeoPoint, km: Double) {
        radiusKm = km
        prefetching = true
        lastPrefetchCentre = centre
        val started = TilePrefetcher.prefetch(this, binding.map, centre, km) { p ->
            runOnUiThread {
                if (p.finished) {
                    prefetching = false
                    p.message?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
                    renderTileState()
                } else {
                    binding.chipPreload.text = "${p.done}/${p.total}"
                }
            }
        }
        if (started < 0) prefetching = false
    }

    /**
     * Keeps the cached disc ahead of the vehicle. Only fires when the map is allowed on the
     * network, so an explicitly offline session never starts downloading mid-drive.
     */
    private fun maybeAutoPrefetch(status: LoggerStatus) {
        if (offline || prefetching || !status.running) return
        val lat = status.lastLat ?: return
        val lon = status.lastLon ?: return
        val here = GeoPoint(lat, lon)
        if (TilePrefetcher.movedFarEnoughToRefresh(lastPrefetchCentre, here, radiusKm)) {
            startPrefetch(here, radiusKm)
        }
    }

    private fun renderTileState() {
        if (!prefetching) binding.chipPreload.text = getString(R.string.preload)
        binding.chipOffline.text = getString(if (offline) R.string.offline else R.string.online)
        val archives = OfflineMaps.archives(this)
        val vector = MapsforgeSource.mapFiles(this)
        // Only a hard-offline map with nothing on disk is actually blank; online mode can fetch.
        val blank = offline && archives.isEmpty() && vector.isEmpty()
        binding.tvNoTiles.visibility = if (blank) android.view.View.VISIBLE else android.view.View.GONE
        if (blank) {
            binding.tvNoTiles.text =
                "No offline map found.\n\nDrop an .mbtiles archive into\n" +
                    OfflineMaps.baseDir(this).absolutePath +
                    "\n\nor tap Offline to switch to Online and pan over your area once to " +
                    "cache it."
        }
    }

    private fun mapSourceLabel(): String {
        val vector = MapsforgeSource.mapFiles(this)
        return when {
            vector.isNotEmpty() -> vector.joinToString { it.name }
            OfflineMaps.archives(this).isNotEmpty() -> "tile archive"
            offline -> "cached tiles"
            else -> "online tiles"
        }
    }

    // ------------------------------------------------------------------ status

    private fun requestPermissions() {
        val wanted = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        permissionLauncher.launch(wanted.toTypedArray())
    }

    private fun hasFineLocation(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    private fun startRecording() {
        followPosition = true
        ContextCompat.startForegroundService(this, Intent(this, SensorService::class.java))
    }

    private fun render(status: LoggerStatus) {
        binding.chipState.text = when {
            status.error != null -> "Error"
            status.running -> getString(R.string.recording)
            else -> getString(R.string.idle)
        }

        binding.btnRecord.text = when {
            status.running -> getString(R.string.stop)
            hasFineLocation() -> getString(R.string.start)
            else -> getString(R.string.grant_permissions)
        }

        val quality = status.gnssQuality
        binding.chipGnss.setText(
            when (quality) {
                GnssQuality.GOOD -> R.string.gnss_good
                GnssQuality.WEAK -> R.string.gnss_weak
                GnssQuality.LOST -> R.string.gnss_lost
                GnssQuality.IDLE -> R.string.gnss_idle
            }
        )
        binding.chipGnss.setTextColor(
            ContextCompat.getColor(
                this,
                when (quality) {
                    GnssQuality.GOOD -> R.color.quality_good
                    GnssQuality.WEAK -> R.color.quality_weak
                    GnssQuality.LOST -> R.color.quality_lost
                    GnssQuality.IDLE -> R.color.quality_idle
                },
            )
        )

        maybeAutoPrefetch(status)

        binding.legendGps.setTextColor(ContextCompat.getColor(this, R.color.track))
        binding.legendImu.setTextColor(ContextCompat.getColor(this, R.color.track_imu))

        binding.btnFreeRun.setText(if (status.freeRun) R.string.free_run_on else R.string.free_run)

        // Drift is only meaningful once the integrator has a GNSS anchor to have drifted from.
        binding.mDrift.text = when {
            !status.running || status.drLat == null -> "—"
            else -> String.format(Locale.US, "%.0f m", status.driftMetres)
        }
        binding.mDrift.setTextColor(
            ContextCompat.getColor(
                this,
                when {
                    !status.running || status.drLat == null -> R.color.quality_idle
                    status.driftMetres > 100 -> R.color.quality_lost
                    status.driftMetres > 25 -> R.color.quality_weak
                    else -> R.color.quality_good
                },
            )
        )

        if (status.running) {
            val rate =
                if (status.elapsedSeconds > 0) status.imuSamples / status.elapsedSeconds else 0L
            binding.mElapsed.text = formatDuration(status.elapsedSeconds)
            binding.mImu.text = compact(status.imuSamples)
            binding.mFixes.text = status.gpsFixes.toString()
            binding.mSats.text = "${status.satellitesUsedInFix}/${status.satellitesVisible}"
            binding.tvDetail.text = String.format(
                Locale.US,
                "%d Hz Â· C/N0 %.0f dB-Hz Â· %s Â· %s",
                rate,
                status.meanCn0DbHz,
                if (status.secondsSinceFix < 0) "no fix yet" else "fix ${status.secondsSinceFix}s ago",
                status.lastAccuracyM?.let { String.format(Locale.US, "Â±%.0f m", it) } ?: "Â±— m",
            )
        } else {
            binding.mElapsed.text = "—"
            binding.mImu.text = "—"
            binding.mFixes.text = "—"
            binding.mSats.text = "—"
            binding.tvDetail.text = status.error
                ?: status.sessionPath?.let { "Last session: " + it.substringAfterLast('/') }
                ?: "Sessions are written to Android/data/$packageName/files/sessions/"
        }
        
        // Rotate the marker to point in the direction the device is pointing
        status.deviceAzimuth?.let { azimuth ->
            marker.rotation = -azimuth // osmdroid rotations might need negation based on the map's orientation, we'll try -azimuth or azimuth
            drMarker.rotation = -azimuth
        }
    }

    private fun formatDuration(seconds: Long): String =
        if (seconds < 3600) String.format(Locale.US, "%d:%02d", seconds / 60, seconds % 60)
        else String.format(Locale.US, "%d:%02d:%02d", seconds / 3600, (seconds % 3600) / 60, seconds % 60)

    private fun compact(n: Long): String = when {
        n >= 1_000_000 -> String.format(Locale.US, "%.1fM", n / 1_000_000.0)
        n >= 1_000 -> String.format(Locale.US, "%.1fk", n / 1_000.0)
        else -> n.toString()
    }

    private fun copyMapFromAssetsIfNeeded() {
        val baseDir = OfflineMaps.baseDir(this)
        if (!baseDir.exists()) baseDir.mkdirs()
        val mapFile = java.io.File(baseDir, "eastern-zone.map")
        if (!mapFile.exists()) {
            lifecycleScope.launch(kotlinx.coroutines.Dispatchers.IO) {
                try {
                    assets.open("eastern-zone.map").use { input ->
                        java.io.FileOutputStream(mapFile).use { output ->
                            input.copyTo(output)
                        }
                    }
                    launch(kotlinx.coroutines.Dispatchers.Main) {
                        Toast.makeText(this@MainActivity, "Offline map extracted successfully.", Toast.LENGTH_SHORT).show()
                        if (::binding.isInitialized) {
                            val bounds = OfflineMaps.apply(binding.map, offline)
                            bounds?.let { binding.map.post { binding.map.zoomToBoundingBox(it, false) } }
                            binding.map.invalidate()
                            renderTileState()
                        }
                    }
                } catch (e: Exception) {
                    android.util.Log.e("MainActivity", "Failed to extract map from assets", e)
                }
            }
        }
    }
}
