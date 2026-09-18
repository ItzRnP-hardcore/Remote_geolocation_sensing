package com.example.imulogger

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PathMeasure
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import android.view.animation.LinearInterpolator
import androidx.core.content.ContextCompat
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * The figure-eight compass gesture, shown rather than described.
 *
 * A phone glyph travels a lemniscate at the tempo the gesture should actually be done at, and
 * turns to follow the path's tangent. Its width breathes with the curve so it reads as the
 * phone *rolling* over at each lobe - which is the part people miss when they only wave it
 * side to side, and the part that gives the magnetometer readings from new directions.
 *
 * Underneath, the path fills in from [progress], which the caller feeds from
 * [CalibrationTracker] - so the fill is real coverage of field directions, not a timer. When
 * [complete] is set the glyph stops at the crossing and the path turns to the success colour.
 *
 * Honours the system animation scale: with animations off, ValueAnimator jumps to its end
 * value and the view draws the static path with the glyph at rest.
 */
class Figure8View @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    /** Share of the path filled in, 0..1. */
    var progress: Float = 0f
        set(value) {
            field = value.coerceIn(0f, 1f)
            invalidate()
        }

    var complete: Boolean = false
        set(value) {
            if (field == value) return
            field = value
            if (value) animator.cancel() else if (isAttachedToWindow) animator.start()
            invalidate()
        }

    private val density = resources.displayMetrics.density

    private val trackPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = 6f * density
        color = ContextCompat.getColor(context, R.color.overlay_stroke)
    }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = 6f * density
        color = ContextCompat.getColor(context, R.color.brand)
    }
    private val phoneBody = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.on_overlay_primary)
    }
    private val phoneScreen = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.brand)
    }
    private val successColor = ContextCompat.getColor(context, R.color.quality_good)

    private val path = Path()
    private val filled = Path()
    private val measure = PathMeasure()
    private var pathLength = 0f
    private val pos = FloatArray(2)
    private val tan = FloatArray(2)
    private val body = RectF()
    private val screen = RectF()

    /** Position along the lemniscate, radians, driven by [animator]. */
    private var phase = 0f

    private val animator = ValueAnimator.ofFloat(0f, (2 * Math.PI).toFloat()).apply {
        // ~2.4 s per loop is a comfortable, thorough pace for the real gesture; faster and
        // people flick, slower and they lose patience before covering enough directions.
        duration = 2400L
        repeatCount = ValueAnimator.INFINITE
        interpolator = LinearInterpolator()
        addUpdateListener {
            phase = it.animatedValue as Float
            invalidate()
        }
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        if (!complete) animator.start()
    }

    override fun onDetachedFromWindow() {
        animator.cancel()
        super.onDetachedFromWindow()
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        buildPath(w.toFloat(), h.toFloat())
    }

    /** Lemniscate of Bernoulli, fitted to the view with room for the glyph at the lobes. */
    private fun buildPath(w: Float, h: Float) {
        path.reset()
        val cx = w / 2f
        val cy = h / 2f
        val a = min(w * 0.42f, h * 0.9f)
        val steps = 240
        for (i in 0..steps) {
            val t = (i.toFloat() / steps) * 2f * Math.PI.toFloat()
            val (x, y) = lemniscate(t, a)
            if (i == 0) path.moveTo(cx + x, cy + y) else path.lineTo(cx + x, cy + y)
        }
        path.close()
        measure.setPath(path, false)
        pathLength = measure.length
    }

    private fun lemniscate(t: Float, a: Float): Pair<Float, Float> {
        val s = sin(t)
        val c = cos(t)
        val d = 1f + s * s
        return Pair(a * c / d, a * s * c / d)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (pathLength <= 0f) return

        canvas.drawPath(path, trackPaint)

        fillPaint.color = if (complete) successColor
        else ContextCompat.getColor(context, R.color.brand)
        filled.reset()
        measure.getSegment(0f, pathLength * (if (complete) 1f else progress), filled, true)
        canvas.drawPath(filled, fillPaint)

        // The glyph. At rest on the crossing once complete; otherwise riding the path.
        val at = if (complete) 0.25f * pathLength
        else (phase / (2f * Math.PI.toFloat())) * pathLength
        measure.getPosTan(at, pos, tan)
        val angle = Math.toDegrees(atan2(tan[1], tan[0]).toDouble()).toFloat()

        // Width "breathes" with |cos| of the phase: narrow at the lobe tips, where the real
        // gesture rolls the phone onto its edge, full-width through the crossing.
        val roll = if (complete) 1f else 0.35f + 0.65f * abs(cos(phase))
        val ph = 30f * density
        val pw = 16f * density * roll

        canvas.save()
        canvas.translate(pos[0], pos[1])
        canvas.rotate(angle + 90f)
        body.set(-pw / 2f, -ph / 2f, pw / 2f, ph / 2f)
        canvas.drawRoundRect(body, 4f * density * roll, 4f * density, phoneBody)
        val inset = 2f * density * roll
        screen.set(body.left + inset, body.top + 3f * density, body.right - inset,
            body.bottom - 3f * density)
        canvas.drawRoundRect(screen, 2f * density * roll, 2f * density, phoneScreen)
        canvas.restore()
    }
}
