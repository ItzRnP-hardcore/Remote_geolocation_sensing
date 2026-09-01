package com.example.imulogger

import android.content.Context
import android.util.Log
import org.pytorch.IValue
import org.pytorch.LiteModuleLoader
import org.pytorch.Module
import org.pytorch.Tensor
import java.io.File
import java.io.FileOutputStream

/**
 * Runs the ResNet1D displacement model over a sliding window of levelled IMU samples.
 *
 * Not thread-safe by design: construct it and call [processIMUData] from one thread only. In this
 * app that is the ML thread owned by [SensorService], which keeps a ~40 ms inference off both the
 * main thread and the 200 Hz logging thread.
 *
 * The window is [SEQUENCE_LENGTH] samples fed at 10 Hz, i.e. 10 seconds — the rate and span the
 * model was trained on. Feeding it at the raw sensor rate would give the network a 0.5 s window
 * and inputs it has never seen.
 */
class IMUModelRunner(context: Context) {

    companion object {
        private const val TAG = "IMUModelRunner"
        const val SEQUENCE_LENGTH = 100
        const val NUM_FEATURES = 6
        const val MODEL_ASSET = "model_mobile.pt"

        /** Result indices, matching the tuple order the export produces. */
        const val IDX_MU = 0
        const val IDX_LOGVAR = 1
        const val IDX_STATIONARY = 2
        const val IDX_YAW_RATE = 3
    }

    private val module: Module = LiteModuleLoader.load(assetFilePath(context, MODEL_ASSET))

    /**
     * Flat ring buffer, oldest sample first once full. An ArrayList with removeAt(0) shifts every
     * element on each sample; at 10 Hz that is survivable but pointless.
     */
    private val ring = FloatArray(SEQUENCE_LENGTH * NUM_FEATURES)
    private var writeIndex = 0
    private var filled = 0

    /** Reused across calls; the tensor copies out of it, so one buffer is enough. */
    private val ordered = FloatArray(SEQUENCE_LENGTH * NUM_FEATURES)
    private val result = FloatArray(4)

    /**
     * Feed one levelled sample (earth-frame acceleration XYZ, gyro XYZ).
     *
     * Returns `[mu, logvar, stationary_logit, yaw_rate]` once the window is full, else null. The
     * returned array is reused — copy it if you need to keep it.
     */
    fun processIMUData(
        eax: Float, eay: Float, eaz: Float,
        gx: Float, gy: Float, gz: Float,
    ): FloatArray? {
        val base = writeIndex * NUM_FEATURES
        ring[base] = eax
        ring[base + 1] = eay
        ring[base + 2] = eaz
        ring[base + 3] = gx
        ring[base + 4] = gy
        ring[base + 5] = gz

        writeIndex = (writeIndex + 1) % SEQUENCE_LENGTH
        if (filled < SEQUENCE_LENGTH) {
            filled++
            return null
        }

        // Unwrap the ring into chronological order. writeIndex now points at the oldest sample.
        val split = writeIndex * NUM_FEATURES
        System.arraycopy(ring, split, ordered, 0, ring.size - split)
        System.arraycopy(ring, 0, ordered, ring.size - split, split)

        return try {
            val input = Tensor.fromBlob(
                ordered,
                longArrayOf(1, SEQUENCE_LENGTH.toLong(), NUM_FEATURES.toLong()),
            )
            val output = module.forward(IValue.from(input))
            if (!output.isTuple) {
                Log.e(TAG, "Model returned ${output.javaClass.simpleName}, expected a tuple")
                return null
            }
            val tuple = output.toTuple()
            if (tuple.size != 4) {
                Log.e(TAG, "Model returned ${tuple.size} outputs, expected 4")
                return null
            }
            for (i in 0 until 4) result[i] = tuple[i].toTensor().dataAsFloatArray[0]
            result
        } catch (e: Exception) {
            Log.e(TAG, "Inference failed", e)
            null
        }
    }

    /** Discard the window. Call between sessions so a new run does not inherit stale samples. */
    fun reset() {
        writeIndex = 0
        filled = 0
    }

    /**
     * PyTorch Lite loads from a filesystem path, so the asset has to be materialised once.
     *
     * The copy is skipped when the sizes already match. Rewriting 15 MB on every service start is
     * pure latency, and the size check still picks up a rebuilt model because retraining changes
     * the serialised length in practice — delete the app's data if you ever need to force it.
     */
    private fun assetFilePath(context: Context, assetName: String): String {
        val file = File(context.filesDir, assetName)
        // openFd only works on stored (uncompressed) assets — see noCompress in build.gradle.kts.
        // If that ever regresses, fall back to copying every time rather than failing to load.
        val assetLength = try {
            context.assets.openFd(assetName).use { it.length }
        } catch (e: Exception) {
            Log.w(TAG, "$assetName is compressed in the APK; cannot size-check, recopying", e)
            -1L
        }
        if (assetLength > 0 && file.exists() && file.length() == assetLength) {
            return file.absolutePath
        }

        val tmp = File(context.filesDir, "$assetName.tmp")
        context.assets.open(assetName).use { input ->
            FileOutputStream(tmp).use { out ->
                input.copyTo(out, 64 * 1024)
                out.flush()
                out.fd.sync()
            }
        }
        // Rename only once the bytes are down, so a kill mid-copy cannot leave a truncated model
        // that the size check would later accept.
        if (!tmp.renameTo(file)) {
            tmp.delete()
            throw IllegalStateException("Could not move $assetName into place")
        }
        Log.i(TAG, "Extracted $assetName ($assetLength bytes)")
        return file.absolutePath
    }
}
