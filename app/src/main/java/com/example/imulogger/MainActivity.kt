package com.example.imulogger

import android.Manifest
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
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
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {

    private companion object {
        const val MAP_ASSET = "eastern-zone.map"

        /** How much ground the locate button frames around the current position. */
        const val CENTRE_RADIUS_KM = 1.0
    }


    private lateinit var binding: ActivityMainBinding
    private lateinit var trackLine: Polyline
    private lateinit var drLine: Polyline
    private lateinit var snapLine: Polyline
    private lateinit var marker: Marker
    private lateinit var drMarker: Marker
    private lateinit var locationOverlay: MyLocationNewOverlay

    /** Overlays repainting the degraded stretches of each track; rebuilt whenever it grows. */
    private val gpsQualityLines = mutableListOf<Polyline>()
    private val drQualityLines = mutableListOf<Polyline>()

    private var panelExpanded = false
    private var offline = true
    private var followPosition = true
    private var trackSize = 0
    private var drSize = 0
    private var snapSize = 0

    private var radiusKm = 10.0
    private var lastPrefetchCentre: GeoPoint? = null
    private var prefetching = false

    /** One promotion at a time: onResume and the completion broadcast can fire back to back. */
    private val promoting = AtomicBoolean(false)

    /** Set while the settings sheet is open, so a finished install can refresh its rows. */
    private var settingsRefresh: (() -> Unit)? = null

    /**
     * Fires when DownloadManager finishes any download while the activity is visible. Without
     * it a map that lands while the user is looking at the map only appears after the next
     * onResume, which is exactly the moment they are not going to trigger.
     */
    private val downloadReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == DownloadManager.ACTION_DOWNLOAD_COMPLETE) promoteDownloads()
        }
    }

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

        binding.btnSettings.setOnClickListener { showSettings() }
        binding.btnEmptyDownload.setOnClickListener { showSettings() }
        binding.tvEmptyPath.text = "Or drop a .map file into " + OfflineMaps.baseDir(this).absolutePath

        binding.btnTheme.setOnClickListener {
            MapsforgeSource.setNight(this, !MapsforgeSource.isNight(this))
            reattachMap()
            Toast.makeText(
                this,
                if (MapsforgeSource.isNight(this)) "Night map" else "Day map",
                Toast.LENGTH_SHORT,
            ).show()
        }

        binding.btnFreeRun.setOnClickListener {
            SensorService.setFreeRun(!SensorService.status.value.freeRun)
        }

        binding.fabCentre.setOnClickListener { centreOnMe() }

        binding.fabPanel.setOnClickListener { setPanelExpanded(!panelExpanded) }
        setPanelExpanded(false)

        // Insets rather than a hardcoded 48dp: the status bar is a different height on other
        // devices and in landscape, and the chips end up either clipped or floating.
        val chipGap = (16 * resources.displayMetrics.density).toInt()
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(binding.topBar) { view, insets ->
            val statusBar =
                insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars()).top
            view.setPadding(view.paddingLeft, statusBar + chipGap, view.paddingRight, chipGap)
            insets
        }

        // The UI follows the service, not the last tap: if recording stops on its own, the
        // button and chips correct themselves.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch { SensorService.status.collect { render(it) } }
                launch { SensorService.track.collect { drawTrack(it) } }
                launch { SensorService.drTrack.collect { drawDrTrack(it) } }
                launch { SensorService.snapTrack.collect { drawSnapTrack(it) } }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        // A system broadcast, so the receiver has to be exported for Android 14+ to deliver it.
        ContextCompat.registerReceiver(
            this,
            downloadReceiver,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            ContextCompat.RECEIVER_EXPORTED,
        )
    }

    override fun onResume() {
        super.onResume()
        binding.map.onResume()
        // The common case: the download finished while the app was in the background or dead.
        promoteDownloads()
    }

    override fun onPause() {
        binding.map.onPause()
        super.onPause()
    }

    override fun onStop() {
        unregisterReceiver(downloadReceiver)
        super.onStop()
    }

    // ------------------------------------------------------------------ map

    private fun setUpMap() = with(binding.map) {
        val vectorBounds = OfflineMaps.apply(this, offline)
        setMultiTouchControls(true)
        zoomController.setVisibility(
            org.osmdroid.views.CustomZoomButtonsController.Visibility.NEVER
        )
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
        // The snapped track sits under both so the raw tracks stay readable over it.
        snapLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track_snap)
            outlinePaint.strokeWidth = 12f
            outlinePaint.alpha = 200
        }
        marker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position)
        }
        drMarker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position_imu)
        }
        overlays.add(snapLine)
        overlays.add(trackLine)
        overlays.add(drLine)
        overlays.add(drMarker)
        overlays.add(marker)
        // With no fix yet, framing the data we actually have beats staring at null island.
        vectorBounds?.let { post { zoomToBoundingBox(it, false) } }
        renderTileState()
    }

    /**
     * Show or hide the session panel.
     *
     * The panel is diagnostics; the map is the app. Collapsed to a button by default so the map
     * gets the screen, and the button carries the record state so nothing important is hidden by
     * being collapsed.
     */
    private fun setPanelExpanded(expanded: Boolean) {
        panelExpanded = expanded
        androidx.transition.TransitionManager.beginDelayedTransition(
            binding.root as android.view.ViewGroup,
            androidx.transition.AutoTransition().apply { duration = 180 },
        )
        binding.panelCard.visibility = if (expanded) android.view.View.VISIBLE else android.view.View.GONE
        binding.fabPanel.setImageResource(if (expanded) R.drawable.ic_close else R.drawable.ic_panel)
        binding.fabPanel.contentDescription =
            getString(if (expanded) R.string.hide_panel else R.string.show_panel)
    }

    /**
     * Bring the current position into view, tightening to [CENTRE_RADIUS_KM] only when the map is
     * currently showing more than that.
     *
     * Zooming unconditionally would pull the user back out every time they had deliberately zoomed
     * in past a kilometre, so a closer view is treated as intentional and only panned.
     */
    private fun centreOnMe() {
        followPosition = true
        if (::locationOverlay.isInitialized) locationOverlay.enableFollowLocation()

        val here = (if (::locationOverlay.isInitialized) locationOverlay.myLocation else null)
            ?: SensorService.status.value.let { s ->
                if (s.lastLat != null && s.lastLon != null) GeoPoint(s.lastLat, s.lastLon) else null
            }

        if (here == null) {
            Toast.makeText(this, "No position yet — waiting for a GPS fix.", Toast.LENGTH_SHORT).show()
            return
        }

        val visible = visibleRadiusKm()
        if (visible == null || visible > CENTRE_RADIUS_KM) {
            // Asking for a box rather than a zoom level keeps the visible span honest across
            // screen sizes: the same numeric zoom covers very different ground on a tall phone.
            val box = TilePrefetcher.boundingBox(here, CENTRE_RADIUS_KM)
            // post() because zoomToBoundingBox needs the view measured; on the first tap after
            // launch it is not, and the zoom silently lands on the wrong level.
            binding.map.post { binding.map.zoomToBoundingBox(box, true) }
        } else {
            binding.map.controller.animateTo(here)
        }
    }

    /**
     * Half the shorter visible span, in kilometres — the same "radius" convention
     * [TilePrefetcher.boundingBox] uses, so the two are directly comparable. Null before the map
     * has been laid out and its bounding box is meaningless.
     */
    private fun visibleRadiusKm(): Double? {
        if (binding.map.width == 0 || binding.map.height == 0) return null
        val box = binding.map.boundingBox ?: return null
        val latSpanKm = box.latitudeSpan * 111.132
        val lonSpanKm =
            box.longitudeSpanWithDateLine * 111.320 *
                kotlin.math.cos(Math.toRadians(box.centerLatitude)).coerceAtLeast(0.01)
        if (latSpanKm <= 0 || lonSpanKm <= 0) return null
        return minOf(latSpanKm, lonSpanKm) / 2.0
    }

    private fun drawTrack(points: List<TrackPoint>) {
        if (points.size == trackSize) return
        trackSize = points.size
        trackLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        rebuildQualitySegments(points, trackLine, gpsQualityLines, 11f)
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
        rebuildQualitySegments(points, drLine, drQualityLines, 10f)
        points.lastOrNull()?.let { drMarker.position = GeoPoint(it.lat, it.lon) }
        binding.map.invalidate()
    }

    /**
     * Repaint the stretches where GNSS was degraded or withheld, on top of the base track.
     *
     * The two tracks diverging is the whole story, and a viewer cannot see *why* unless the
     * cause is drawn too. Rather than a multi-coloured polyline (osmdroid has no such thing),
     * contiguous runs of non-GOOD points become their own overlays laid over the base line,
     * starting one point early so they visually join the healthy track either side.
     */
    private fun drawSnapTrack(points: List<TrackPoint>) {
        if (points.size == snapSize) return
        snapSize = points.size
        snapLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        binding.map.invalidate()
    }

    private fun rebuildQualitySegments(
        points: List<TrackPoint>,
        baseLine: Polyline,
        store: MutableList<Polyline>,
        widthPx: Float,
    ) {
        val overlays = binding.map.overlays
        store.forEach { overlays.remove(it) }
        store.clear()

        var i = 0
        while (i < points.size) {
            val quality = points[i].quality
            if (quality == GnssQuality.GOOD || quality == GnssQuality.IDLE) {
                i++
                continue
            }
            var j = i
            while (j < points.size && points[j].quality == quality) j++
            val from = (i - 1).coerceAtLeast(0)
            val to = (j - 1).coerceAtMost(points.size - 1)
            if (to > from) {
                store.add(
                    Polyline(binding.map).apply {
                        setPoints(points.subList(from, to + 1).map { GeoPoint(it.lat, it.lon) })
                        outlinePaint.color = ContextCompat.getColor(
                            this@MainActivity,
                            if (quality == GnssQuality.LOST) R.color.quality_lost
                            else R.color.quality_weak,
                        )
                        outlinePaint.strokeWidth = widthPx
                    }
                )
            }
            i = j
        }

        // Insert directly above the base line so markers and the other track stay on top.
        val at = (overlays.indexOf(baseLine) + 1).coerceIn(0, overlays.size)
        store.forEachIndexed { index, line -> overlays.add(at + index, line) }
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
        val archives = OfflineMaps.archives(this)
        val vector = MapsforgeSource.mapFiles(this)
        // Only a hard-offline map with nothing on disk is actually blank; online mode can fetch.
        val blank = offline && archives.isEmpty() && vector.isEmpty()
        binding.cardEmpty.visibility = if (blank) android.view.View.VISIBLE else android.view.View.GONE
    }

    /** Rebuild the tile provider after the theme or the set of installed maps changed. */
    private fun reattachMap() {
        val bounds = OfflineMaps.apply(binding.map, offline)
        binding.map.invalidate()
        renderTileState()
        // Only reframe when there is nowhere better to look; otherwise keep the user's view.
        if (SensorService.status.value.lastLat == null && trackSize == 0) {
            bounds?.let { binding.map.post { binding.map.zoomToBoundingBox(it, false) } }
        }
    }

    /**
     * Move any finished map download into place and, if one landed, rebuild the map on it.
     *
     * Runs on its own thread rather than lifecycleScope: the copy fallback can take several
     * seconds on a half-gigabyte file, and cancelling it because the user rotated the screen
     * would leave the download un-promoted until the next resume, which is the bug this exists
     * to fix. The UI work at the end checks the activity is still alive.
     */
    private fun promoteDownloads() {
        if (!MapDownloader.hasCompletedDownloads(this)) return
        if (!promoting.compareAndSet(false, true)) return
        settingsRefresh?.invoke() // show "Installing" straight away
        val appContext = applicationContext
        Thread({
            val promoted = try {
                MapDownloader.promoteCompleted(appContext)
            } catch (e: Exception) {
                android.util.Log.e("MainActivity", "Map promotion failed", e)
                emptyList()
            } finally {
                promoting.set(false)
            }
            runOnUiThread {
                if (isFinishing || isDestroyed || !::binding.isInitialized) return@runOnUiThread
                if (promoted.isNotEmpty()) {
                    reattachMap()
                    announceInstalled(promoted)
                }
                settingsRefresh?.invoke()
            }
        }, "map-install").start()
    }

    /**
     * Say which zones landed and offer to look at them. The map only reframes itself when there
     * is nothing else to show, so after a download during a session the new region would
     * otherwise be installed but invisible.
     */
    private fun announceInstalled(zones: List<MapDownloader.Zone>) {
        val bounds = zones
            .mapNotNull { MapsforgeSource.info(MapDownloader.installedFile(this, it))?.bounds }
        val bar = com.google.android.material.snackbar.Snackbar.make(
            binding.root,
            "Offline map installed: " + zones.joinToString { it.shortLabel },
            com.google.android.material.snackbar.Snackbar.LENGTH_LONG,
        )
        bar.anchorView = binding.fabCentre
        if (bounds.isNotEmpty()) {
            bar.setAction(R.string.view_map) {
                followPosition = false
                val union = org.osmdroid.util.BoundingBox(
                    bounds.maxOf { it.latNorth }, bounds.maxOf { it.lonEast },
                    bounds.minOf { it.latSouth }, bounds.minOf { it.lonWest },
                )
                binding.map.zoomToBoundingBox(union, true)
            }
        }
        bar.show()
        updateCoverageHint(SensorService.status.value)
    }

    /**
     * Warn when the position is known but no installed map contains it. This is the confusion
     * the app used to leave unexplained: the GNSS chip says "good", the map says nothing.
     */
    private fun updateCoverageHint(status: LoggerStatus) {
        val chip = binding.chipCoverage
        val here = when {
            status.lastLat != null && status.lastLon != null -> GeoPoint(status.lastLat, status.lastLon)
            ::locationOverlay.isInitialized -> locationOverlay.myLocation
            else -> null
        }
        if (here == null || MapsforgeSource.covering(this, here.latitude, here.longitude) != null) {
            chip.visibility = android.view.View.GONE
            return
        }
        val zone = MapDownloader.suggestZone(here.latitude, here.longitude)
            ?.takeIf { !MapDownloader.isInstalled(this, it) }
        chip.text = if (zone != null) "No offline map here · Download ${zone.shortLabel} zone"
        else getString(R.string.coverage_none)
        chip.setOnClickListener {
            if (zone != null) confirmDownload(zone) { showSettings() } else showSettings()
        }
        chip.visibility = android.view.View.VISIBLE
    }

    // ------------------------------------------------------------------ settings

    private fun showSettings() {
        val sheet = com.google.android.material.bottomsheet.BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.dialog_settings, null)
        sheet.setContentView(view)

        val swOffline = view.findViewById<com.google.android.material.materialswitch.MaterialSwitch>(R.id.swOffline)
        swOffline.isChecked = offline
        swOffline.setOnCheckedChangeListener { _, checked ->
            offline = checked
            reattachMap()
        }

        val advanced = view.findViewById<android.view.View>(R.id.advancedGroup)
        val btnAdvanced = view.findViewById<android.widget.TextView>(R.id.btnAdvanced)
        btnAdvanced.setOnClickListener {
            val open = advanced.visibility != android.view.View.VISIBLE
            advanced.visibility = if (open) android.view.View.VISIBLE else android.view.View.GONE
            btnAdvanced.setText(if (open) R.string.advanced_expanded else R.string.advanced_collapsed)
        }

        view.findViewById<android.widget.Button>(R.id.btnTileSource).setOnClickListener {
            sheet.dismiss()
            promptForTileSource()
        }
        view.findViewById<android.widget.Button>(R.id.btnPreload).setOnClickListener {
            sheet.dismiss()
            askPrefetchRadius()
        }

        val list = view.findViewById<android.widget.LinearLayout>(R.id.zoneList)
        val storage = view.findViewById<android.widget.TextView>(R.id.tvStorage)

        // DownloadManager exposes no progress callback, so the sheet polls — but only while a
        // transfer is actually in flight. Ticking unconditionally would keep the window from ever
        // going idle, which burns battery and blocks UI automation from ever settling.
        val ticker = object : Runnable {
            override fun run() {
                promoteDownloads()
                bindZoneRows(list, storage) { rescheduleTicker(list, this) }
                rescheduleTicker(list, this)
            }
        }
        settingsRefresh = { refreshRows(list, storage) }
        bindZoneRows(list, storage) { list.post(ticker) }
        rescheduleTicker(list, ticker)
        sheet.setOnDismissListener {
            list.removeCallbacks(ticker)
            settingsRefresh = null
        }
        sheet.show()
    }

    private fun bindZoneRows(
        list: android.widget.LinearLayout,
        storage: android.widget.TextView,
        refresh: () -> Unit,
    ) {
        list.removeAllViews()
        var installedBytes = 0L

        for (zone in MapDownloader.INDIA_ZONES) {
            val row = layoutInflater.inflate(R.layout.item_map_zone, list, false)
            val name = row.findViewById<android.widget.TextView>(R.id.zoneName)
            val status = row.findViewById<android.widget.TextView>(R.id.zoneStatus)
            val action = row.findViewById<com.google.android.material.button.MaterialButton>(R.id.zoneAction)
            val bar = row.findViewById<com.google.android.material.progressindicator.LinearProgressIndicator>(R.id.zoneProgress)

            name.text = zone.label
            val installed = MapDownloader.isInstalled(this, zone)
            val progress = MapDownloader.progress(this, zone)
            val installingNow = zone.id in MapDownloader.installing ||
                progress?.status == DownloadManager.STATUS_SUCCESSFUL

            when {
                installingNow -> {
                    status.text = getString(R.string.installing)
                    action.text = getString(R.string.installing)
                    action.isEnabled = false
                    // Verifying and moving: no byte count to show, so an indeterminate bar.
                    // Mode must be set before the bar becomes visible or Material throws.
                    bar.isIndeterminate = true
                    bar.visibility = android.view.View.VISIBLE
                }
                progress != null && progress.running -> {
                    val sized = progress.bytesTotal > 0
                    status.text = if (sized) String.format(
                        Locale.US, "%d%%  %d / %d MB",
                        progress.percent,
                        progress.bytesDone / 1_048_576,
                        progress.bytesTotal / 1_048_576,
                    ) else if (progress.status == DownloadManager.STATUS_PAUSED) "Paused, waiting for network"
                    else "Starting…"
                    bar.isIndeterminate = !sized
                    bar.visibility = android.view.View.VISIBLE
                    if (sized) bar.setProgressCompat(progress.percent, false)
                    action.text = getString(R.string.cancel)
                    action.setOnClickListener {
                        MapDownloader.cancel(this, zone)
                        refreshRows(list, storage)
                    }
                }
                installed -> {
                    val file = MapDownloader.installedFile(this, zone)
                    installedBytes += file.length()
                    val info = MapsforgeSource.info(file)
                    status.text = if (info != null) String.format(
                        Locale.US, "Installed · %d MB · OSM data %s",
                        info.sizeBytes / 1_048_576,
                        java.text.SimpleDateFormat("d MMM yyyy", Locale.US).format(java.util.Date(info.dataDateMs)),
                    ) else getString(R.string.installed)
                    status.setTextColor(ContextCompat.getColor(this, R.color.quality_good))
                    action.text = getString(R.string.delete)
                    action.setOnClickListener { confirmDelete(zone) { refreshRows(list, storage) } }
                }
                else -> {
                    status.text = String.format(Locale.US, "about %d MB", zone.approxMb)
                    action.text = getString(R.string.download)
                    action.setOnClickListener { confirmDownload(zone) { refreshRows(list, storage) } }
                }
            }
            list.addView(row)
        }

        storage.text = String.format(
            Locale.US, "%d MB installed on this device", installedBytes / 1_048_576,
        )
    }

    /** Keep polling only while at least one download is live. */
    private fun rescheduleTicker(list: android.widget.LinearLayout, ticker: Runnable) {
        list.removeCallbacks(ticker)
        val active = MapDownloader.INDIA_ZONES.any {
            MapDownloader.progress(this, it)?.running == true
        }
        if (active) list.postDelayed(ticker, 1_000)
    }

    private fun refreshRows(list: android.widget.LinearLayout, storage: android.widget.TextView) {
        bindZoneRows(list, storage) {}
    }

    /**
     * Network choice is put to the user rather than defaulted, because these files run to half a
     * gigabyte and that is their data allowance to spend.
     */
    private fun confirmDownload(zone: MapDownloader.Zone, onChanged: () -> Unit) {
        MaterialAlertDialogBuilder(this)
            .setTitle(zone.label)
            .setMessage(
                "About ${zone.approxMb} MB from download.mapsforge.org (OpenStreetMap data). " +
                    "The download resumes if interrupted and continues while the app is closed."
            )
            .setPositiveButton("Wi-Fi only") { _, _ ->
                MapDownloader.enqueue(this, zone, wifiOnly = true); onChanged()
            }
            .setNeutralButton("Any network") { _, _ ->
                MapDownloader.enqueue(this, zone, wifiOnly = false); onChanged()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun confirmDelete(zone: MapDownloader.Zone, onChanged: () -> Unit) {
        MaterialAlertDialogBuilder(this)
            .setTitle("Delete ${zone.label}?")
            .setMessage("Frees about ${zone.approxMb} MB. You can download it again later.")
            .setPositiveButton(R.string.delete) { _, _ ->
                MapDownloader.delete(this, zone)
                reattachMap()
                onChanged()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
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

        binding.fabPanel.backgroundTintList = android.content.res.ColorStateList.valueOf(
            ContextCompat.getColor(
                this,
                when {
                    status.error != null -> R.color.action_armed
                    status.freeRun && status.running -> R.color.action_armed
                    status.running -> R.color.action_record
                    else -> R.color.overlay_chip
                },
            )
        )
        if (status.error != null && !panelExpanded) setPanelExpanded(true)

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
        updateCoverageHint(status)

        binding.tvMatchSource.text = when {
            status.running && status.matchMap != null -> "Map matching · ${status.matchMap}"
            status.running -> getString(R.string.match_off)
            else -> MapsforgeSource.mapFiles(this).firstOrNull()
                ?.let { "Map matching will use ${it.name}" }
                ?: getString(R.string.match_off)
        }

        binding.legendGps.setTextColor(ContextCompat.getColor(this, R.color.track))
        binding.legendImu.setTextColor(ContextCompat.getColor(this, R.color.track_imu))
        binding.legendSnap.setTextColor(ContextCompat.getColor(this, R.color.track_snap))
        binding.legendSnap.text = if (status.running && status.snapLat != null) {
            String.format(
                Locale.US, "Snapped %s %.0f m (%.0f%%)", "·",
                status.snapCorrectionM, 100 * status.snapConfidence,
            )
        } else {
            getString(R.string.legend_snap)
        }
        // Folding live speed into the legend gives the most legible real-time proof that
        // something is working, without another row of chrome.
        binding.legendGps.text =
            if (status.running && status.lastSpeedMps != null) {
                String.format(Locale.US, "GPS %s %.0f km/h", "·", status.lastSpeedMps * 3.6f)
            } else {
                getString(R.string.legend_gps)
            }
        binding.legendImu.text =
            if (status.running && status.drLat != null) {
                String.format(Locale.US, "IMU %s %.0f km/h", "·", status.drSpeedMps * 3.6)
            } else {
                getString(R.string.legend_imu)
            }

        binding.btnFreeRun.setText(if (status.freeRun) R.string.free_run_on else R.string.free_run)
        binding.btnFreeRun.setStrokeColorResource(
            if (status.freeRun) R.color.action_armed else R.color.overlay_stroke_strong
        )
        binding.btnFreeRun.setTextColor(
            ContextCompat.getColor(
                this,
                if (status.freeRun) R.color.action_armed else R.color.on_overlay_primary,
            )
        )

        // Drift is only meaningful once the integrator has a GNSS anchor to have drifted from.
        binding.mDrift.text = when {
            !status.running || status.drLat == null -> "—"
            else -> String.format(Locale.US, "%.0f m", status.driftMetres)
        }
        // Colour against the plan's benchmark once there is enough distance for the ratio to
        // mean anything, and fall back to absolute thresholds before that.
        val pct = status.driftPercent
        binding.mDrift.setTextColor(
            ContextCompat.getColor(
                this,
                when {
                    !status.running || status.drLat == null -> R.color.quality_idle
                    pct != null && pct > DRIFT_BENCHMARK_PERCENT -> R.color.quality_lost
                    pct != null && pct > DRIFT_BENCHMARK_PERCENT / 2 -> R.color.quality_weak
                    pct != null -> R.color.quality_good
                    status.driftMetres > 100 -> R.color.quality_lost
                    status.driftMetres > 25 -> R.color.quality_weak
                    else -> R.color.quality_good
                },
            )
        )

        binding.mDriftPercent.text = when {
            !status.running || status.drLat == null -> ""
            pct == null -> String.format(
                Locale.US, "of %s travelled", formatDistance(status.distanceMetres),
            )
            else -> String.format(
                Locale.US,
                "%.1f%% of %s  %s",
                pct,
                formatDistance(status.distanceMetres),
                if (pct <= DRIFT_BENCHMARK_PERCENT) "✓ under 10%" else "✗ over 10%",
            )
        }
        binding.mDriftPercent.visibility =
            if (binding.mDriftPercent.text.isNullOrEmpty()) android.view.View.GONE
            else android.view.View.VISIBLE
        binding.mDriftPercent.setTextColor(
            ContextCompat.getColor(
                this,
                when {
                    pct == null -> R.color.on_overlay_secondary
                    pct <= DRIFT_BENCHMARK_PERCENT -> R.color.quality_good
                    else -> R.color.quality_lost
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
                "%d Hz · C/N0 %.0f dB-Hz · %s · %s",
                rate,
                status.meanCn0DbHz,
                if (status.secondsSinceFix < 0) "no fix yet" else "fix ${status.secondsSinceFix}s ago",
                status.lastAccuracyM?.let { String.format(Locale.US, "±%.0f m", it) } ?: "±— m",
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

    private fun formatDistance(metres: Double): String =
        if (metres >= 1000) String.format(Locale.US, "%.2f km", metres / 1000.0)
        else String.format(Locale.US, "%.0f m", metres)

    private fun formatDuration(seconds: Long): String =
        if (seconds < 3600) String.format(Locale.US, "%d:%02d", seconds / 60, seconds % 60)
        else String.format(Locale.US, "%d:%02d:%02d", seconds / 3600, (seconds % 3600) / 60, seconds % 60)

    private fun compact(n: Long): String = when {
        n >= 1_000_000 -> String.format(Locale.US, "%.1fM", n / 1_000_000.0)
        n >= 1_000 -> String.format(Locale.US, "%.1fk", n / 1_000.0)
        else -> n.toString()
    }

    /**
     * Materialise the bundled Mapsforge map, which is 220 MB and therefore takes a while.
     *
     * Extraction goes to a temporary file and is renamed into place only once every byte is down.
     * Writing straight to the destination means a kill mid-copy — or simply backing out of the
     * activity, which cancels this coroutine — leaves a truncated file that `exists()` happily
     * accepts forever, after which Mapsforge fails to parse it and the app silently falls back to
     * blank raster tiles with nothing on screen to say why.
     */
    private fun copyMapFromAssetsIfNeeded() {
        val baseDir = OfflineMaps.baseDir(this)
        if (!baseDir.exists()) baseDir.mkdirs()
        val mapFile = java.io.File(baseDir, MAP_ASSET)

        val assetLength = try {
            assets.openFd(MAP_ASSET).use { it.length }
        } catch (e: Exception) {
            android.util.Log.w("MainActivity", "Cannot size $MAP_ASSET", e)
            -1L
        }
        // No bundled asset in this build: whatever is on disk was downloaded or side-loaded by
        // the user, and deleting it here would be exactly the "my map vanished" bug.
        if (assetLength <= 0) return
        // A file of exactly the right length is the one case we can skip.
        if (mapFile.exists() && mapFile.length() == assetLength) return
        // A different length is fine too if it is a real Mapsforge file: the user downloaded a
        // newer build of the same zone from the server, and it must not be clobbered.
        if (mapFile.exists() && MapDownloader.hasMapsforgeHeader(mapFile)) return
        if (mapFile.exists()) {
            android.util.Log.w(
                "MainActivity",
                "Removing ${mapFile.name}: ${mapFile.length()} bytes, expected $assetLength",
            )
            mapFile.delete()
        }

        // Deliberately not lifecycleScope: a 220 MB copy must not be cancelled halfway because
        // the user rotated the screen or stepped into another app.
        val appContext = applicationContext
        Thread({
            val tmp = java.io.File(baseDir, "$MAP_ASSET.tmp")
            try {
                appContext.assets.open(MAP_ASSET).use { input ->
                    java.io.FileOutputStream(tmp).use { output ->
                        input.copyTo(output, 1 shl 16)
                        output.flush()
                        output.fd.sync()
                    }
                }
                if (assetLength > 0 && tmp.length() != assetLength) {
                    throw java.io.IOException("copied ${tmp.length()} of $assetLength bytes")
                }
                if (!tmp.renameTo(mapFile)) throw java.io.IOException("rename failed")

                runOnUiThread {
                    if (isFinishing || isDestroyed || !::binding.isInitialized) return@runOnUiThread
                    Toast.makeText(this, "Offline map ready.", Toast.LENGTH_SHORT).show()
                    val bounds = OfflineMaps.apply(binding.map, offline)
                    bounds?.let { binding.map.post { binding.map.zoomToBoundingBox(it, false) } }
                    binding.map.invalidate()
                    renderTileState()
                }
            } catch (e: Exception) {
                tmp.delete()
                android.util.Log.e("MainActivity", "Failed to extract map from assets", e)
                runOnUiThread {
                    if (isFinishing || isDestroyed) return@runOnUiThread
                    Toast.makeText(
                        this,
                        "Could not extract the offline map: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }, "map-extract").start()
    }
}
