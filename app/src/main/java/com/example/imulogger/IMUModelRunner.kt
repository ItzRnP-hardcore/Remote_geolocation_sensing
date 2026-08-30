package com.example.imulogger

import android.content.Context
import org.pytorch.IValue
import org.pytorch.LiteModuleLoader
import org.pytorch.Module
import org.pytorch.Tensor
import java.io.File
import java.io.FileOutputStream

class IMUModelRunner(context: Context) {
    private var module: Module? = null
    private val sequenceLength = 50
    private val numFeatures = 8
    private val imuBuffer = mutableListOf<FloatArray>()

    init {
        // Load the model from assets
        val modelPath = assetFilePath(context, "model_mobile.pt")
        module = LiteModuleLoader.load(modelPath)
    }

    /**
     * Feed new processed IMU data (earth_ax, earth_ay, earth_az, gx, gy, gz, gps_speed, gps_bearing)
     * Returns the predicted compensation (delta_v, delta_theta) if buffer is full, else null.
     */
    fun processIMUData(
        eax: Float, eay: Float, eaz: Float,
        gx: Float, gy: Float, gz: Float,
        speed: Float, bearing: Float
    ): FloatArray? {
        imuBuffer.add(floatArrayOf(eax, eay, eaz, gx, gy, gz, speed, bearing))
        
        // Keep only the last `sequenceLength` items
        if (imuBuffer.size > sequenceLength) {
            imuBuffer.removeAt(0)
        }

        if (imuBuffer.size == sequenceLength) {
            return runInference()
        }
        return null
    }

    private fun runInference(): FloatArray? {
        module?.let {
            // Flatten the 2D buffer into a 1D float array for the tensor
            val flatArray = FloatArray(sequenceLength * numFeatures)
            var index = 0
            for (i in 0 until sequenceLength) {
                for (j in 0 until numFeatures) {
                    flatArray[index++] = imuBuffer[i][j]
                }
            }

            // Create tensor of shape (1, 50, 8)
            val tensor = Tensor.fromBlob(flatArray, longArrayOf(1, sequenceLength.toLong(), numFeatures.toLong()))

            // Run inference
            val outputTensor = it.forward(IValue.from(tensor)).toTensor()
            return outputTensor.dataAsFloatArray
        }
        return null
    }

    private fun assetFilePath(context: Context, assetName: String): String {
        val file = File(context.filesDir, assetName)
        if (file.exists() && file.length() > 0) {
            return file.absolutePath
        }
        context.assets.open(assetName).use { `is` ->
            FileOutputStream(file).use { os ->
                val buffer = ByteArray(4 * 1024)
                var read: Int
                while (`is`.read(buffer).also { read = it } != -1) {
                    os.write(buffer, 0, read)
                }
                os.flush()
            }
        }
        return file.absolutePath
    }
}
