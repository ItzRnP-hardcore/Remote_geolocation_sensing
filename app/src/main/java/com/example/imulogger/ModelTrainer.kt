package com.example.imulogger

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import org.pytorch.IValue
import org.pytorch.LiteModuleLoader
import org.pytorch.Module
import org.pytorch.Tensor
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * Handles On-Device Training (Fine-Tuning) in the background.
 * Collects 10-minute chunks of data and updates the model.
 */
class ModelTrainer(private val context: Context) {

    companion object {
        private const val TAG = "ModelTrainer"
        const val TRAIN_MODEL_ASSET = "model_mobile_train.pt"
        const val TUNED_WEIGHTS_FILE = "tuned_weights.bin"
        
        // 10 minutes * 60 seconds * 10 Hz = 6000 windows 
        // (assuming 1 window per sample step, though overlapping)
        const val CHUNK_SIZE = 6000 
    }

    // A simple data class to hold paired training data
    data class TrainingSample(
        val inputOrdered: FloatArray, // (SEQUENCE_LENGTH * NUM_FEATURES)
        val targetDisp: Float,
        val targetStat: Float,
        val targetYaw: Float
    )

    private val trainingQueue = ConcurrentLinkedQueue<TrainingSample>()
    private var isTraining = false
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())

    // The module used for training
    private var trainModule: Module? = null

    init {
        loadModel()
    }

    private fun loadModel() {
        try {
            val assetPath = assetFilePath(context, TRAIN_MODEL_ASSET)
            trainModule = LiteModuleLoader.load(assetPath)
            Log.i(TAG, "Loaded base training model from assets.")
            
            val weightsFile = File(context.filesDir, TUNED_WEIGHTS_FILE)
            if (weightsFile.exists()) {
                loadWeights(weightsFile)
                Log.i(TAG, "Loaded tuned weights from disk and applied to model.")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load training model", e)
        }
    }

    private fun loadWeights(file: File) {
        val module = trainModule ?: return
        try {
            val existingWeightsIValue = module.runMethod("get_weights")
            val existingTensors = existingWeightsIValue.toTensorList()
            
            val newTensors = mutableListOf<Tensor>()
            
            DataInputStream(FileInputStream(file)).use { dis ->
                for (existingTensor in existingTensors) {
                    val shape = existingTensor.shape()
                    val numElements = shape.fold(1L) { acc, i -> acc * i }.toInt()
                    
                    val floatArray = FloatArray(numElements)
                    for (i in 0 until numElements) {
                        floatArray[i] = dis.readFloat()
                    }
                    
                    newTensors.add(Tensor.fromBlob(floatArray, shape))
                }
            }
            
            module.runMethod("set_weights", IValue.listFrom(*newTensors.toTypedArray()))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load tuned weights", e)
        }
    }

    private fun saveWeights() {
        val module = trainModule ?: return
        val file = File(context.filesDir, TUNED_WEIGHTS_FILE)
        val tmp = File(context.filesDir, "${TUNED_WEIGHTS_FILE}.tmp")
        try {
            val weightsIValue = module.runMethod("get_weights")
            val tensors = weightsIValue.toTensorList()
            
            DataOutputStream(FileOutputStream(tmp)).use { dos ->
                for (tensor in tensors) {
                    val floatArray = tensor.dataAsFloatArray
                    for (value in floatArray) {
                        dos.writeFloat(value)
                    }
                }
            }
            if (tmp.renameTo(file)) {
                Log.i(TAG, "Successfully saved tuned weights to disk.")
            } else {
                tmp.delete()
                Log.e(TAG, "Failed to rename tmp file for weights.")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to save tuned weights", e)
            tmp.delete()
        }
    }

    /**
     * Enqueue data collected by IMUModelRunner and GPS.
     */
    fun queueData(sample: TrainingSample) {
        trainingQueue.add(sample)
        
        if (trainingQueue.size >= CHUNK_SIZE && !isTraining) {
            Log.i(TAG, "Collected enough samples for a 10 min chunk. Starting background training.")
            startTrainingChunk()
        }
    }

    private fun startTrainingChunk() {
        isTraining = true
        val module = trainModule ?: return

        scope.launch {
            try {
                Log.i(TAG, "Training chunk started...")
                val chunk = mutableListOf<TrainingSample>()
                for (i in 0 until CHUNK_SIZE) {
                    val s = trainingQueue.poll()
                    if (s != null) chunk.add(s) else break
                }

                if (chunk.isEmpty()) {
                    isTraining = false
                    return@launch
                }

                // NOTE: PyTorch Mobile Java doesn't easily support batching dynamic arrays into Tensors
                // without manually flattening them. For on-device, we iterate and train one-by-one or 
                // in small mini-batches. Doing one-by-one for simplicity:
                
                var totalLoss = 0f
                for (sample in chunk) {
                    val inputTensor = Tensor.fromBlob(
                        sample.inputOrdered,
                        longArrayOf(1, IMUModelRunner.SEQUENCE_LENGTH.toLong(), IMUModelRunner.NUM_FEATURES.toLong())
                    )
                    
                    val dispTensor = Tensor.fromBlob(floatArrayOf(sample.targetDisp), longArrayOf(1))
                    val statTensor = Tensor.fromBlob(floatArrayOf(sample.targetStat), longArrayOf(1))
                    val yawTensor = Tensor.fromBlob(floatArrayOf(sample.targetYaw), longArrayOf(1))

                    module.runMethod("zero_grad")
                    
                    val lossOutput = module.runMethod(
                        "forward_train", 
                        IValue.from(inputTensor), 
                        IValue.from(dispTensor),
                        IValue.from(statTensor),
                        IValue.from(yawTensor)
                    )
                    
                    totalLoss += lossOutput.toTensor().dataAsFloatArray[0]
                    
                    module.runMethod("backward")
                    module.runMethod("step")
                }

                Log.i(TAG, "Training chunk finished. Avg Loss: \${totalLoss / chunk.size}")
                
                // Save the updated weights to disk
                saveWeights()

            } catch (e: Exception) {
                Log.e(TAG, "Error during training", e)
            } finally {
                isTraining = false
            }
        }
    }

    /**
     * Called when the app is quitting to ensure any pending training finishes.
     */
    fun finishPendingTraining() = runBlocking {
        if (isTraining || trainingQueue.isNotEmpty()) {
            Log.i(TAG, "App quitting: finishing pending training first...")
            startTrainingChunk()
            // Wait briefly for coroutines
            delay(2000) 
        }
    }

    private fun assetFilePath(context: Context, assetName: String): String {
        val file = File(context.filesDir, assetName)
        val tmp = File(context.filesDir, "${assetName}.tmp")
        context.assets.open(assetName).use { input ->
            java.io.FileOutputStream(tmp).use { out ->
                input.copyTo(out, 64 * 1024)
                out.flush()
                out.fd.sync()
            }
        }
        if (!tmp.renameTo(file)) {
            tmp.delete()
        }
        return file.absolutePath
    }
}
