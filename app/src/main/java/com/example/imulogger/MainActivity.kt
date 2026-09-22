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
import android.content.res.ColorStateList
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
import java.io.File

import android.annotation.SuppressLint
import android.location.Location
import android.location.LocationManager
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.view.HapticFeedbackConstants
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.card.MaterialCardView
import android.text.Editable
import android.text.TextWatcher
import android.view.inputmethod.InputMethodManager
import android.graphics.Color
import android.graphics.Paint
import android.widget.ImageView
import android.widget.Button
import kotlinx.coroutines.delay
import org.osmdroid.events.MapEventsReceiver
import org.osmdroid.views.overlay.MapEventsOverlay
import org.osmdroid.util.BoundingBox

class MainActivity : AppCompatActivity() {

    private companion object {
        const val MAP_ASSET = "eastern-zone.map"

        /** How much ground the locate button frames around the current position. */
        const val CENTRE_RADIUS_KM = 1.0
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var bottomSheetBehavior: BottomSheetBehavior<MaterialCardView>
    private lateinit var trackLine: Polyline
    private lateinit var drLine: Polyline
    private lateinit var snapLine: Polyline
    private lateinit var routeLine: Polyline
    private var destinationMarker: Marker? = null
    private var activeDestination: GeoPoint? = null
    private var activeDestinationName: String? = null
    private var roadNetwork: RoadNetwork? = null
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

    private var showGpsTrack = true
    private var showDrTrack = true
    private var showSnapTrack = true
    private var isViewingHistory = false
    private var historySession: SessionManager.LoadedSession? = null
    private var currentMarkerRotation = 0f
    private var isHeadingUpMode = false
    private var currentMapOrientation = 0f
    private var latestKnownAzimuth = 0f
    private var lastGnssQuality: GnssQuality? = null

    private var sensorManager: SensorManager? = null
    private var rotationSensor: Sensor? = null
    private val rotationMatrix = FloatArray(9)
    private val orientationAngles = FloatArray(3)

    private val compassListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val azimuthDeg: Float? = when (event.sensor.type) {
                Sensor.TYPE_ROTATION_VECTOR, Sensor.TYPE_GEOMAGNETIC_ROTATION_VECTOR -> {
                    SensorManager.getRotationMatrixFromVector(rotationMatrix, event.values)
                    SensorManager.getOrientation(rotationMatrix, orientationAngles)
                    Math.toDegrees(orientationAngles[0].toDouble()).toFloat()
                }
                Sensor.TYPE_ORIENTATION -> event.values[0]
                else -> null
            }

            if (azimuthDeg != null) {
                applyHeading(azimuthDeg)
            }

            // Keep marker positioned if locationOverlay found coordinates first
            if (marker.position == null && ::locationOverlay.isInitialized && locationOverlay.myLocation != null) {
                val loc = locationOverlay.myLocation
                marker.position = loc
                drMarker.position = loc
                binding.map.invalidate()
            }
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var locationWatcherRegistered = false
    private var hasInitialCentered = false

    private val locationReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            checkAndRefreshLocation()
        }
    }

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
            checkAndRefreshLocation()
            showCalibrationDialog()
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

        val prefs = getSharedPreferences("imu_prefs", Context.MODE_PRIVATE)
        SensorService.useMagnetometerYaw = prefs.getBoolean("use_magnetometer_yaw", false)
        SensorService.useCompassHeading = prefs.getBoolean("use_compass_heading", false)
        SensorService.isPhoneFixed = prefs.getBoolean("is_phone_fixed", true)

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        rotationSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
            ?: sensorManager?.getDefaultSensor(Sensor.TYPE_GEOMAGNETIC_ROTATION_VECTOR)
            ?: sensorManager?.getDefaultSensor(Sensor.TYPE_ORIENTATION)

        setUpMap()

        // Modern Material 3 Bottom Sheet setup
        bottomSheetBehavior = BottomSheetBehavior.from(binding.panelCard).apply {
            state = BottomSheetBehavior.STATE_COLLAPSED
            isHideable = false
            addBottomSheetCallback(object : BottomSheetBehavior.BottomSheetCallback() {
                override fun onStateChanged(bottomSheet: View, newState: Int) {
                    panelExpanded = (newState == BottomSheetBehavior.STATE_EXPANDED)
                }
                override fun onSlide(bottomSheet: View, slideOffset: Float) {}
            })
        }

        binding.btnRecord.setOnClickListener {
            hapticClick()
            if (SensorService.status.value.running) {
                stopService(Intent(this, SensorService::class.java))
            } else if (hasFineLocation()) {
                showCalibrationDialog()
            } else {
                requestPermissions()
            }
        }

        binding.btnHistory.setOnClickListener { showHistoryDialog() }
        binding.btnExitHistory.setOnClickListener { exitHistoryMode() }

        binding.btnSettings.setOnClickListener { showSettings() }
        binding.btnEmptyDownload.setOnClickListener { showSettings() }
        binding.tvEmptyPath.text = "Or drop a .map file into " + OfflineMaps.baseDir(this).absolutePath

        binding.btnTheme.setOnClickListener {
            hapticClick()
            MapsforgeSource.setNight(this, !MapsforgeSource.isNight(this))
            reattachMap()
            Toast.makeText(
                this,
                if (MapsforgeSource.isNight(this)) "Night map" else "Day map",
                Toast.LENGTH_SHORT,
            ).show()
        }

        binding.btnFreeRun.setOnClickListener {
            hapticClick()
            SensorService.setFreeRun(!SensorService.status.value.freeRun)
        }

        binding.fabCentre.setOnClickListener {
            hapticClick()
            centreOnMe()
        }

        binding.btnToggleOrientation.setOnClickListener {
            toggleHeadingUpMode()
        }

        setupLayerToggles()
        setupDestinationSearch()

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
                launch { SensorService.azimuth.collect { applyHeading(it) } }
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

        val locationFilter = IntentFilter(LocationManager.PROVIDERS_CHANGED_ACTION).apply {
            addAction(LocationManager.MODE_CHANGED_ACTION)
        }
        ContextCompat.registerReceiver(
            this,
            locationReceiver,
            locationFilter,
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        locationWatcherRegistered = true
    }

    override fun onResume() {
        super.onResume()
        binding.map.onResume()
        // The common case: the download finished while the app was in the background or dead.
        promoteDownloads()
        checkAndRefreshLocation()
        rotationSensor?.let {
            sensorManager?.registerListener(compassListener, it, SensorManager.SENSOR_DELAY_GAME)
        }
    }

    override fun onPause() {
        sensorManager?.unregisterListener(compassListener)
        binding.map.onPause()
        super.onPause()
    }

    override fun onStop() {
        if (locationWatcherRegistered) {
            try {
                unregisterReceiver(locationReceiver)
            } catch (_: Exception) {}
            locationWatcherRegistered = false
        }
        unregisterReceiver(downloadReceiver)
        super.onStop()
    }

    // ------------------------------------------------------------------ map

    private fun setUpMap() = with(binding.map) {
        setLayerType(View.LAYER_TYPE_HARDWARE, null)
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

        val provider = GpsMyLocationProvider(context).apply {
            addLocationSource(LocationManager.NETWORK_PROVIDER)
            addLocationSource(LocationManager.GPS_PROVIDER)
        }
        locationOverlay = MyLocationNewOverlay(provider, this)
        
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
        locationOverlay.runOnFirstFix {
            runOnUiThread {
                val myLoc = locationOverlay.myLocation
                if (myLoc != null && marker.position == null) {
                    marker.position = myLoc
                    drMarker.position = myLoc
                    if (followPosition && !isViewingHistory) {
                        binding.map.controller.animateTo(myLoc)
                    }
                    binding.map.invalidate()
                }
            }
        }
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

        // Navigation route polyline
        routeLine = Polyline(this).apply {
            outlinePaint.color = Color.parseColor("#00E5FF")
            outlinePaint.strokeWidth = 14f
            outlinePaint.strokeCap = Paint.Cap.ROUND
            outlinePaint.strokeJoin = Paint.Join.ROUND
        }
        destinationMarker = Marker(binding.map).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_BOTTOM)
            icon = ContextCompat.getDrawable(this@MainActivity, R.drawable.ic_destination_pin)
            title = getString(R.string.destination)
        }

        trackLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track)
            outlinePaint.strokeWidth = 8f
        }
        drLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track_imu)
            outlinePaint.strokeWidth = 7f
            outlinePaint.pathEffect = DashPathEffect(floatArrayOf(18f, 12f), 0f)
        }
        snapLine = Polyline(this).apply {
            outlinePaint.color = ContextCompat.getColor(context, R.color.track_snap)
            outlinePaint.strokeWidth = 7f
        }
        marker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position)
        }
        drMarker = Marker(this).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = ContextCompat.getDrawable(context, R.drawable.ic_position_imu)
        }

        val mapEventsOverlay = MapEventsOverlay(object : MapEventsReceiver {
            override fun singleTapConfirmedHelper(p: GeoPoint): Boolean {
                if (binding.rvSearchSuggestions.visibility == View.VISIBLE) {
                    binding.rvSearchSuggestions.visibility = View.GONE
                    hideKeyboard()
                    return true
                }
                return false
            }

            override fun longPressHelper(p: GeoPoint): Boolean {
                hapticClick()
                navigateTo(p, "Pinned Destination")
                return true
            }
        })
        overlays.add(0, mapEventsOverlay)
        overlays.add(routeLine)
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
        if (::bottomSheetBehavior.isInitialized) {
            bottomSheetBehavior.state = if (expanded) {
                BottomSheetBehavior.STATE_EXPANDED
            } else {
                BottomSheetBehavior.STATE_COLLAPSED
            }
        }
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
            if (!isLocationEnabled()) {
                com.google.android.material.snackbar.Snackbar.make(
                    binding.root,
                    "Location services are turned off",
                    com.google.android.material.snackbar.Snackbar.LENGTH_LONG
                ).setAction("Turn On") {
                    startActivity(Intent(android.provider.Settings.ACTION_LOCATION_SOURCE_SETTINGS))
                }.show()
                return
            }
            checkAndRefreshLocation()
            Toast.makeText(this, "Acquiring location fix…", Toast.LENGTH_SHORT).show()
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
        if (isViewingHistory) return
        if (points.size == trackSize) return
        val oldSize = trackSize
        trackSize = points.size
        if (oldSize == 0 || trackLine.actualPoints.isEmpty()) {
            trackLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        } else {
            for (i in oldSize until points.size) {
                val pt = points[i]
                trackLine.addPoint(GeoPoint(pt.lat, pt.lon))
            }
        }
        rebuildQualitySegments(points, trackLine, gpsQualityLines, 11f)
        points.lastOrNull()?.let {
            val here = GeoPoint(it.lat, it.lon)
            marker.position = here
            if (followPosition) binding.map.controller.setCenter(here)
        }
        binding.map.invalidate()
    }

    private fun drawDrTrack(points: List<TrackPoint>) {
        if (isViewingHistory) return
        if (points.size == drSize) return
        val oldSize = drSize
        drSize = points.size
        if (oldSize == 0 || drLine.actualPoints.isEmpty()) {
            drLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        } else {
            for (i in oldSize until points.size) {
                val pt = points[i]
                drLine.addPoint(GeoPoint(pt.lat, pt.lon))
            }
        }
        rebuildQualitySegments(points, drLine, drQualityLines, 10f)
        points.lastOrNull()?.let { drMarker.position = GeoPoint(it.lat, it.lon) }
        binding.map.invalidate()
    }

    /**
     * Repaint the stretches where GNSS was degraded or withheld, on top of the base track.
     */
    private fun drawSnapTrack(points: List<TrackPoint>) {
        if (isViewingHistory) return
        if (points.size == snapSize) return
        val oldSize = snapSize
        snapSize = points.size
        if (oldSize == 0 || snapLine.actualPoints.isEmpty()) {
            snapLine.setPoints(points.map { GeoPoint(it.lat, it.lon) })
        } else {
            for (i in oldSize until points.size) {
                val pt = points[i]
                snapLine.addPoint(GeoPoint(pt.lat, pt.lon))
            }
        }
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

        val swHeadingUp = view.findViewById<com.google.android.material.materialswitch.MaterialSwitch>(R.id.swHeadingUp)
        swHeadingUp?.isChecked = isHeadingUpMode
        swHeadingUp?.setOnCheckedChangeListener { _, checked ->
            if (checked != isHeadingUpMode) {
                toggleHeadingUpMode(checked)
            }
        }
        view.findViewById<View>(R.id.cardOrientation)?.setOnClickListener {
            toggleHeadingUpMode()
            swHeadingUp?.isChecked = isHeadingUpMode
        }

        val swMagYaw = view.findViewById<com.google.android.material.materialswitch.MaterialSwitch>(R.id.swMagnetometerYaw)
        swMagYaw?.isChecked = SensorService.useMagnetometerYaw
        swMagYaw?.setOnCheckedChangeListener { _, checked ->
            // One switch, both halves: the compass heading is only meaningful if the
            // integrator's attitude shares its north, so the two never diverge.
            SensorService.useMagnetometerYaw = checked
            SensorService.useCompassHeading = checked
            getSharedPreferences("imu_prefs", Context.MODE_PRIVATE).edit()
                .putBoolean("use_magnetometer_yaw", checked)
                .putBoolean("use_compass_heading", checked)
                .apply()
        }
        view.findViewById<View>(R.id.cardMagYaw)?.setOnClickListener {
            swMagYaw?.toggle()
        }

        val swVehicle = view.findViewById<com.google.android.material.materialswitch.MaterialSwitch>(R.id.swVehicleMode)
        swVehicle?.isChecked = SensorService.isPhoneFixed
        swVehicle?.setOnCheckedChangeListener { _, checked ->
            SensorService.isPhoneFixed = checked
            getSharedPreferences("imu_prefs", Context.MODE_PRIVATE).edit()
                .putBoolean("is_phone_fixed", checked).apply()
        }
        view.findViewById<View>(R.id.cardVehicleMode)?.setOnClickListener {
            swVehicle?.toggle()
        }

        view.findViewById<View>(R.id.cardHistory).setOnClickListener {
            sheet.dismiss()
            showHistoryDialog()
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

        if (status.error != null && !panelExpanded) setPanelExpanded(true)

        val speedKmh = if (status.running && status.lastSpeedMps != null) status.lastSpeedMps * 3.6f else 0f
        binding.tvPeekSpeed.text = String.format(Locale.US, "%.0f km/h", speedKmh)
        binding.tvPeekDrift.text = if (status.running && status.drLat != null) {
            String.format(Locale.US, "drift: %.0f m", status.driftMetres)
        } else {
            "drift: —"
        }

        binding.btnRecord.text = when {
            status.running -> getString(R.string.stop)
            hasFineLocation() -> getString(R.string.start)
            else -> getString(R.string.grant_permissions)
        }

        val quality = status.gnssQuality
        if (quality == GnssQuality.GOOD && lastGnssQuality != GnssQuality.GOOD) {
            binding.chipGnss.animate().scaleX(1.08f).scaleY(1.08f).setDuration(150).withEndAction {
                binding.chipGnss.animate().scaleX(1.0f).scaleY(1.0f).setDuration(150).start()
            }.start()
        }
        lastGnssQuality = quality
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

        activeDestination?.let { dest ->
            val currLat = status.lastLat ?: status.drLat ?: marker.position?.latitude
            val currLon = status.lastLon ?: status.drLon ?: marker.position?.longitude
            if (currLat != null && currLon != null) {
                val remM = NavigationRouter.haversine(currLat, currLon, dest.latitude, dest.longitude)
                val distStr = formatDistance(remM)
                val sp = status.lastSpeedMps?.toDouble() ?: status.drSpeedMps
                val timeStr = if (sp > 1.0) formatDuration((remM / sp).toLong()) else "~"
                binding.tvNavStats.text = "$distStr remaining · ~$timeStr"
            }
        }

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
        // Smooth rotation is updated continuously at 25 Hz via SensorService.azimuth
    }

    private fun setupLayerToggles() {
        updateLayerToggleUI()
        binding.btnToggleGps.setOnClickListener {
            hapticClick()
            showGpsTrack = !showGpsTrack
            updateLayerToggleUI()
            trackLine.isEnabled = showGpsTrack
            gpsQualityLines.forEach { it.isEnabled = showGpsTrack }
            binding.map.invalidate()
        }
        binding.btnToggleDr.setOnClickListener {
            hapticClick()
            showDrTrack = !showDrTrack
            updateLayerToggleUI()
            drLine.isEnabled = showDrTrack
            drQualityLines.forEach { it.isEnabled = showDrTrack }
            binding.map.invalidate()
        }
        binding.btnToggleSnap.setOnClickListener {
            hapticClick()
            showSnapTrack = !showSnapTrack
            updateLayerToggleUI()
            snapLine.isEnabled = showSnapTrack
            binding.map.invalidate()
        }
    }

    private fun updateLayerToggleUI() {
        binding.btnToggleGps.alpha = if (showGpsTrack) 1.0f else 0.35f
        binding.btnToggleDr.alpha = if (showDrTrack) 1.0f else 0.35f
        binding.btnToggleSnap.alpha = if (showSnapTrack) 1.0f else 0.35f
    }

    private fun applyHeading(azimuthDeg: Float) {
        if (isViewingHistory) return
        latestKnownAzimuth = azimuthDeg

        if (isHeadingUpMode) {
            // HEADING-UP MODE:
            // Location compass marker stays fixed pointing straight UP (0° relative to screen)
            if (marker.rotation != 0f || drMarker.rotation != 0f) {
                marker.rotation = 0f
                drMarker.rotation = 0f
            }

            // The map rotates in the opposite direction (-azimuthDeg):
            val targetMapOrientation = (-azimuthDeg + 360f) % 360f
            var diff = (targetMapOrientation - currentMapOrientation) % 360f
            if (diff > 180f) diff -= 360f
            if (diff < -180f) diff += 360f

            // Low-pass exponential smoothing for fluid motion
            currentMapOrientation = (currentMapOrientation + diff * 0.35f + 360f) % 360f
            binding.map.setMapOrientation(currentMapOrientation, false)

            // Compass needle rotates to show true North
            binding.ivCompassNeedle.rotation = currentMapOrientation

            // Center location on screen while following
            if (followPosition) {
                val center = marker.position
                    ?: (if (::locationOverlay.isInitialized) locationOverlay.myLocation else null)
                if (center != null) {
                    binding.map.controller.setCenter(center)
                }
            }
            binding.map.invalidate()
        } else {
            // NORTH-UP MODE:
            // Smoothly return map orientation to 0° if rotated
            if (currentMapOrientation != 0f) {
                var diff = (0f - currentMapOrientation) % 360f
                if (diff > 180f) diff -= 360f
                if (diff < -180f) diff += 360f
                if (Math.abs(diff) < 1f) {
                    currentMapOrientation = 0f
                } else {
                    currentMapOrientation = (currentMapOrientation + diff * 0.35f + 360f) % 360f
                }
                binding.map.setMapOrientation(currentMapOrientation, false)
            }
            binding.ivCompassNeedle.rotation = 0f

            // Location marker rotates to reflect device compass heading
            val targetMarkerRotation = (-azimuthDeg + 360f) % 360f
            var diff = (targetMarkerRotation - currentMarkerRotation) % 360f
            if (diff > 180f) diff -= 360f
            if (diff < -180f) diff += 360f
            currentMarkerRotation = (currentMarkerRotation + diff * 0.35f + 360f) % 360f
            marker.rotation = currentMarkerRotation
            drMarker.rotation = currentMarkerRotation
            binding.map.invalidate()
        }
    }

    private fun toggleHeadingUpMode(enabled: Boolean? = null) {
        hapticClick()
        val newState = enabled ?: !isHeadingUpMode
        isHeadingUpMode = newState

        binding.switchMapOrientation.isChecked = newState
        binding.tvOrientationMode.text = if (newState) {
            getString(R.string.heading_up_mode)
        } else {
            getString(R.string.north_up_mode)
        }

        binding.btnToggleOrientation.backgroundTintList = ColorStateList.valueOf(
            ContextCompat.getColor(
                this,
                if (newState) R.color.brand_surface else R.color.overlay_card_glass
            )
        )

        if (newState) {
            followPosition = true
            val here = marker.position
                ?: (if (::locationOverlay.isInitialized) locationOverlay.myLocation else null)
            if (here != null) {
                binding.map.controller.setCenter(here)
            }
            Toast.makeText(this, "Heading-Up: Map rotates with heading", Toast.LENGTH_SHORT).show()
        } else {
            currentMapOrientation = 0f
            binding.map.setMapOrientation(0f, true)
            binding.ivCompassNeedle.rotation = 0f
            Toast.makeText(this, "North-Up: Map fixed to North", Toast.LENGTH_SHORT).show()
        }

        applyHeading(latestKnownAzimuth)
    }

    private fun hapticClick() {
        binding.root.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
    }

    private fun showHistoryDialog() {
        hapticClick()
        val sheet = BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.dialog_history, null)
        sheet.setContentView(view)

        val rv = view.findViewById<RecyclerView>(R.id.rvSessions)
        val loading = view.findViewById<View>(R.id.historyLoading)
        val empty = view.findViewById<View>(R.id.emptyHistoryView)
        val tvCount = view.findViewById<TextView>(R.id.tvHistoryCount)

        rv.layoutManager = LinearLayoutManager(this)

        lifecycleScope.launch {
            val sessions = SessionManager.listSessions(this@MainActivity)
            loading.visibility = View.GONE
            if (sessions.isEmpty()) {
                empty.visibility = View.VISIBLE
                tvCount.text = "0 sessions"
            } else {
                empty.visibility = View.GONE
                tvCount.text = "${sessions.size} session${if (sessions.size > 1) "s" else ""}"
                rv.adapter = SessionHistoryAdapter(sessions) { selected ->
                    sheet.dismiss()
                    loadAndDisplayHistory(selected)
                }
            }
        }
        sheet.show()
    }

    private fun loadAndDisplayHistory(summary: SessionManager.SessionSummary) {
        lifecycleScope.launch {
            Toast.makeText(this@MainActivity, "Loading session ${summary.id}…", Toast.LENGTH_SHORT).show()
            val loaded = SessionManager.loadSession(summary)
            isViewingHistory = true
            historySession = loaded
            followPosition = false

            // Draw loaded tracks
            trackLine.setPoints(loaded.gpsTrack.map { GeoPoint(it.lat, it.lon) })
            drLine.setPoints(loaded.drTrack.map { GeoPoint(it.lat, it.lon) })
            snapLine.setPoints(loaded.snapTrack.map { GeoPoint(it.lat, it.lon) })
            rebuildQualitySegments(loaded.gpsTrack, trackLine, gpsQualityLines, 11f)

            // Reset map orientation to 0 (North-Up) so historical track is framed upright
            binding.map.setMapOrientation(0f, false)

            // Hide live current position markers during history review
            marker.isEnabled = false
            drMarker.isEnabled = false

            // Frame bounds
            loaded.bounds?.let { bounds ->
                binding.map.post { binding.map.zoomToBoundingBox(bounds, true) }
            }

            // Show History banner
            binding.tvHistoryTitle.text = "Viewing: ${summary.formattedDate}"
            val durSec = summary.durationSeconds.toLong()
            val durStr = String.format(Locale.US, "%d:%02d", durSec / 60, durSec % 60)
            binding.tvHistoryStats.text = "$durStr · ${compact(summary.imuSamples)} IMU · ${summary.gpsFixes} GPS fixes"
            binding.historyBanner.visibility = View.VISIBLE
            binding.historyRecomputeProgress.visibility = View.GONE
            binding.btnRecompute.isEnabled = true
            binding.btnRecompute.visibility = if (File(summary.dir, "imu.csv").exists()) View.VISIBLE else View.GONE

            binding.btnRecompute.setOnClickListener {
                hapticClick()
                binding.btnRecompute.isEnabled = false
                binding.historyRecomputeProgress.visibility = View.VISIBLE
                binding.historyRecomputeProgress.progress = 0
                binding.tvHistoryStats.text = getString(R.string.recomputing_track)

                lifecycleScope.launch {
                    val result = SessionRecomputer.recomputeSession(
                        context = this@MainActivity,
                        sessionDir = summary.dir,
                        onProgress = { pct ->
                            runOnUiThread {
                                binding.historyRecomputeProgress.progress = pct
                            }
                        }
                    )

                    binding.historyRecomputeProgress.visibility = View.GONE
                    binding.btnRecompute.isEnabled = true

                    if (result != null) {
                        hapticClick()
                        drLine.setPoints(result.recomputedTrack.map { GeoPoint(it.lat, it.lon) })
                        binding.map.invalidate()

                        val origStr = if (result.originalDriftM > 0) String.format(Locale.US, "%.0fm", result.originalDriftM) else "N/A"
                        val newStr = String.format(Locale.US, "%.0fm", result.newDriftM)
                        val pctStr = String.format(Locale.US, "%.0f%%", result.improvementPct)

                        binding.tvHistoryStats.text = "New Drift: $newStr (was $origStr, -$pctStr)"
                        Toast.makeText(
                            this@MainActivity,
                            "Track recomputed! Drift reduced to $newStr (-$pctStr)",
                            Toast.LENGTH_LONG
                        ).show()
                    } else {
                        Toast.makeText(this@MainActivity, getString(R.string.recompute_no_imu), Toast.LENGTH_SHORT).show()
                    }
                }
            }

            binding.map.invalidate()
        }
    }

    private fun exitHistoryMode() {
        hapticClick()
        isViewingHistory = false
        historySession = null
        binding.historyBanner.visibility = View.GONE
        binding.historyRecomputeProgress.visibility = View.GONE

        marker.isEnabled = true
        drMarker.isEnabled = true

        // Restore live or recorded tracks from service
        val liveTrack = SensorService.track.value
        val liveDr = SensorService.drTrack.value
        val liveSnap = SensorService.snapTrack.value
        trackSize = 0
        drSize = 0
        snapSize = 0
        drawTrack(liveTrack)
        drawDrTrack(liveDr)
        drawSnapTrack(liveSnap)

        centreOnMe()
        if (isHeadingUpMode) {
            applyHeading(latestKnownAzimuth)
        }
    }

    private class SessionHistoryAdapter(
        private val items: List<SessionManager.SessionSummary>,
        private val onSelect: (SessionManager.SessionSummary) -> Unit,
    ) : RecyclerView.Adapter<SessionHistoryAdapter.ViewHolder>() {

        class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
            val tvDate: TextView = view.findViewById(R.id.tvSessionDate)
            val tvId: TextView = view.findViewById(R.id.tvSessionId)
            val tvDuration: TextView = view.findViewById(R.id.tvSessionDuration)
            val tvSamples: TextView = view.findViewById(R.id.tvSessionSamples)
            val btnView: View = view.findViewById(R.id.btnViewTrack)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_session_history, parent, false)
            return ViewHolder(view)
        }

        override fun onBindViewHolder(holder: ViewHolder, position: Int) {
            val s = items[position]
            holder.tvDate.text = s.formattedDate
            holder.tvId.text = s.id
            val mins = (s.durationSeconds / 60).toInt()
            val secs = (s.durationSeconds % 60).toInt()
            holder.tvDuration.text = String.format(Locale.US, "%d:%02d duration", mins, secs)
            val imuText = if (s.imuSamples >= 1000) "${s.imuSamples / 1000}k IMU" else "${s.imuSamples} IMU"
            holder.tvSamples.text = "${s.gpsFixes} fixes · $imuText"
            holder.btnView.setOnClickListener { onSelect(s) }
            holder.itemView.setOnClickListener { onSelect(s) }
        }

        override fun getItemCount(): Int = items.size
    }

    private fun isLocationEnabled(): Boolean {
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            lm.isLocationEnabled
        } else {
            lm.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }
    }

    @SuppressLint("MissingPermission")
    private fun checkAndRefreshLocation() {
        if (!hasFineLocation()) return
        if (!isLocationEnabled()) return

        // Re-enable osmdroid's MyLocation overlay so it registers with newly active providers
        if (::locationOverlay.isInitialized) {
            locationOverlay.disableMyLocation()
            locationOverlay.enableMyLocation()
            if (followPosition) locationOverlay.enableFollowLocation()
        }

        // Fetch location immediately via Google Play Services Fused Location
        fusedLocationClient.lastLocation.addOnSuccessListener { loc ->
            if (loc != null) {
                onImmediateLocationFound(loc)
            } else {
                fusedLocationClient.getCurrentLocation(
                    Priority.PRIORITY_HIGH_ACCURACY,
                    null
                ).addOnSuccessListener { freshLoc ->
                    if (freshLoc != null) onImmediateLocationFound(freshLoc)
                }
            }
        }
    }

    private fun onImmediateLocationFound(loc: Location) {
        val here = GeoPoint(loc.latitude, loc.longitude)
        marker.position = here
        if (followPosition && !isViewingHistory) {
            val visible = visibleRadiusKm()
            if (!hasInitialCentered || visible == null || visible > CENTRE_RADIUS_KM) {
                hasInitialCentered = true
                val box = TilePrefetcher.boundingBox(here, CENTRE_RADIUS_KM)
                binding.map.post { binding.map.zoomToBoundingBox(box, true) }
            } else {
                binding.map.controller.animateTo(here)
            }
        }
        binding.map.invalidate()
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

    // ------------------------------------------------------------------ navigation & calibration

    /**
     * Figure-eight compass calibration, before the phone goes on the stand.
     *
     * The animation shows the gesture; [CalibrationTracker] measures it. Continue unlocks only
     * once the magnetometer has been seen from enough directions AND the OS agrees it has a
     * fit, so the result no longer depends on the user guessing whether a compass is "accurate".
     * Choosing the compass also switches the integrator's attitude to the magnetometer-
     * referenced rotation vector: game_rv has no north, and measured on a recorded drive its
     * frame wandered -100 to +48 deg from true, leaking a quarter of the centripetal
     * acceleration into forward speed on every turn.
     */
    private fun showCalibrationDialog() {
        hapticClick()
        val dialogView = layoutInflater.inflate(R.layout.dialog_calibration, null)
        val dialog = BottomSheetDialog(this)
        dialog.setContentView(dialogView)

        val figure8 = dialogView.findViewById<Figure8View>(R.id.figure8)
        val tvProgress = dialogView.findViewById<TextView>(R.id.tvCalibrationProgress)
        val chipAccuracy = dialogView.findViewById<TextView>(R.id.chipAccuracyStatus)
        val btnContinue = dialogView.findViewById<Button>(R.id.btnCompassContinue)
        val btnSkip = dialogView.findViewById<Button>(R.id.btnSkipCompass)

        val tracker = CalibrationTracker()

        fun showAccuracy(accuracy: Int) {
            val (text, color) = when (accuracy) {
                SensorManager.SENSOR_STATUS_ACCURACY_HIGH ->
                    R.string.accuracy_high to R.color.quality_good
                SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM ->
                    R.string.accuracy_medium to R.color.quality_idle
                SensorManager.SENSOR_STATUS_ACCURACY_LOW ->
                    R.string.accuracy_low to R.color.quality_weak
                else -> R.string.accuracy_uncalibrated to R.color.quality_weak
            }
            chipAccuracy.text = getString(text)
            chipAccuracy.setTextColor(ContextCompat.getColor(this@MainActivity, color))
        }

        fun render() {
            figure8.progress = tracker.progress.toFloat()
            if (tracker.done) {
                if (!figure8.complete) {
                    figure8.complete = true
                    hapticClick()
                }
                tvProgress.text = getString(R.string.calibration_progress_done)
                btnContinue.isEnabled = true
                btnContinue.text = getString(R.string.calibration_continue)
            } else {
                tvProgress.text = getString(R.string.calibration_progress,
                    (tracker.progress * 100).toInt())
            }
            showAccuracy(tracker.accuracy)
        }

        val sm = getSystemService(Context.SENSOR_SERVICE) as? SensorManager
        val magSensor = sm?.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)
        val magListener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                tracker.onSample(event.timestamp, event.values[0], event.values[1],
                    event.values[2], event.accuracy)
                render()
            }

            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
                tracker.onAccuracy(accuracy)
                render()
            }
        }
        if (magSensor != null && sm != null) {
            sm.registerListener(magListener, magSensor, SensorManager.SENSOR_DELAY_GAME)
        } else {
            // No magnetometer at all: there is nothing to calibrate, so offer only the gyro.
            btnContinue.isEnabled = false
            tvProgress.text = getString(R.string.accuracy_uncalibrated)
        }

        var cleaned = false
        fun cleanup() {
            if (cleaned) return
            cleaned = true
            if (magSensor != null && sm != null) sm.unregisterListener(magListener)
        }

        fun choose(useCompass: Boolean) {
            hapticClick()
            cleanup()
            dialog.dismiss()
            SensorService.useMagnetometerYaw = useCompass
            SensorService.useCompassHeading = useCompass
            getSharedPreferences("imu_prefs", Context.MODE_PRIVATE).edit()
                .putBoolean("use_magnetometer_yaw", useCompass)
                .putBoolean("use_compass_heading", useCompass)
                .apply()
            startRecording()
        }

        btnContinue.setOnClickListener { choose(useCompass = true) }
        btnSkip.setOnClickListener { choose(useCompass = false) }
        dialog.setOnDismissListener { cleanup() }
        dialog.show()
    }

    private fun setupDestinationSearch() {
        binding.rvSearchSuggestions.layoutManager = LinearLayoutManager(this)
        var searchJob: kotlinx.coroutines.Job? = null

        binding.etSearchDestination.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                val query = s?.toString()?.trim().orEmpty()
                binding.btnClearSearch.visibility = if (query.isNotEmpty()) View.VISIBLE else View.GONE
                searchJob?.cancel()
                if (query.length >= 3) {
                    searchJob = lifecycleScope.launch {
                        delay(350)
                        val loc = marker.position
                            ?: (if (::locationOverlay.isInitialized) locationOverlay.myLocation else null)
                        val results = NavigationRouter.searchPlaces(query, loc?.latitude, loc?.longitude)
                        if (results.isNotEmpty()) {
                            binding.rvSearchSuggestions.visibility = View.VISIBLE
                            binding.rvSearchSuggestions.adapter = SearchSuggestionAdapter(results, loc) { selected ->
                                binding.etSearchDestination.setText(selected.title)
                                binding.rvSearchSuggestions.visibility = View.GONE
                                hideKeyboard()
                                navigateTo(GeoPoint(selected.lat, selected.lon), selected.title)
                            }
                        } else {
                            binding.rvSearchSuggestions.visibility = View.GONE
                        }
                    }
                } else {
                    binding.rvSearchSuggestions.visibility = View.GONE
                }
            }
        })

        binding.btnClearSearch.setOnClickListener {
            hapticClick()
            binding.etSearchDestination.text?.clear()
            binding.rvSearchSuggestions.visibility = View.GONE
        }

        binding.btnExitNav.setOnClickListener {
            hapticClick()
            clearNavigation()
        }
    }

    private fun navigateTo(dest: GeoPoint, title: String) {
        activeDestination = dest
        activeDestinationName = title

        destinationMarker?.let { dm ->
            dm.position = dest
            dm.title = title
            dm.isEnabled = true
            if (!binding.map.overlays.contains(dm)) {
                binding.map.overlays.add(dm)
            }
        }

        binding.navHudCard.visibility = View.VISIBLE
        binding.tvNavTitle.text = title
        binding.tvNavStats.text = "Calculating route…"

        val start = marker.position
            ?: (if (::locationOverlay.isInitialized) locationOverlay.myLocation else null)
            ?: SensorService.status.value.let { s ->
                if (s.lastLat != null && s.lastLon != null) GeoPoint(s.lastLat, s.lastLon) else null
            }
            ?: dest

        lifecycleScope.launch {
            if (roadNetwork == null) {
                roadNetwork = openLocalRoadNetwork()
            }
            val route = NavigationRouter.calculateRoute(
                start = start,
                dest = dest,
                roadNetwork = roadNetwork,
                destinationName = title,
            )
            if (route != null && route.points.isNotEmpty()) {
                routeLine.setPoints(route.points)
                routeLine.isEnabled = true
                val distStr = formatDistance(route.distanceM)
                val method = if (route.isOffline) "Offline A*" else "OSRM"
                binding.tvNavStats.text = "$distStr via $method"

                val maxLat = route.points.maxOf { it.latitude }
                val minLat = route.points.minOf { it.latitude }
                val maxLon = route.points.maxOf { it.longitude }
                val minLon = route.points.minOf { it.longitude }
                val latPad = ((maxLat - minLat) * 0.1).coerceAtLeast(0.002)
                val lonPad = ((maxLon - minLon) * 0.1).coerceAtLeast(0.002)
                val box = BoundingBox(
                    (maxLat + latPad).coerceAtMost(85.0),
                    (maxLon + lonPad).coerceAtMost(180.0),
                    (minLat - latPad).coerceAtLeast(-85.0),
                    (minLon - lonPad).coerceAtLeast(-180.0),
                )
                binding.map.post { binding.map.zoomToBoundingBox(box, true) }
            } else {
                Toast.makeText(this@MainActivity, "Could not compute route to destination", Toast.LENGTH_SHORT).show()
                val remM = NavigationRouter.haversine(start.latitude, start.longitude, dest.latitude, dest.longitude)
                binding.tvNavStats.text = "Direct distance: ${formatDistance(remM)}"
            }
            binding.map.invalidate()
        }
    }

    private fun clearNavigation() {
        activeDestination = null
        activeDestinationName = null
        destinationMarker?.isEnabled = false
        routeLine.setPoints(emptyList())
        routeLine.isEnabled = false
        binding.navHudCard.visibility = View.GONE
        binding.etSearchDestination.text?.clear()
        binding.rvSearchSuggestions.visibility = View.GONE
        hideKeyboard()
        binding.map.invalidate()
    }

    private fun openLocalRoadNetwork(): RoadNetwork? {
        val map = MapsforgeSource.mapFiles(this).firstOrNull() ?: return null
        return try {
            RoadNetwork(map)
        } catch (e: Exception) {
            android.util.Log.w("MainActivity", "Failed opening local RoadNetwork: ${e.message}")
            null
        }
    }

    private fun hideKeyboard() {
        val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager
        val view = currentFocus ?: binding.root
        imm?.hideSoftInputFromWindow(view.windowToken, 0)
    }

    private class SearchSuggestionAdapter(
        private val items: List<NavigationRouter.SearchResult>,
        private val currentLoc: GeoPoint?,
        private val onSelect: (NavigationRouter.SearchResult) -> Unit,
    ) : RecyclerView.Adapter<SearchSuggestionAdapter.ViewHolder>() {

        class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
            val tvTitle: TextView = view.findViewById(R.id.tvPlaceTitle)
            val tvSubtitle: TextView = view.findViewById(R.id.tvPlaceSubtitle)
            val tvDistance: TextView = view.findViewById(R.id.tvPlaceDistance)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_search_suggestion, parent, false)
            return ViewHolder(view)
        }

        override fun onBindViewHolder(holder: ViewHolder, position: Int) {
            val item = items[position]
            holder.tvTitle.text = item.title
            holder.tvSubtitle.text = item.subtitle
            val dist = item.distanceM ?: if (currentLoc != null) {
                NavigationRouter.haversine(currentLoc.latitude, currentLoc.longitude, item.lat, item.lon)
            } else null

            if (dist != null) {
                holder.tvDistance.text = if (dist >= 1000) String.format(Locale.US, "%.1f km", dist / 1000.0)
                else String.format(Locale.US, "%.0f m", dist)
                holder.tvDistance.visibility = View.VISIBLE
            } else {
                holder.tvDistance.visibility = View.GONE
            }
            holder.itemView.setOnClickListener { onSelect(item) }
        }

        override fun getItemCount(): Int = items.size
    }
}
