plugins {
    id("com.android.application")
}

android {
    namespace = "com.example.imulogger"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.example.imulogger"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    androidResources {
        // Leave the Mapsforge map and the TorchScript model stored, not DEFLATE'd. Both are
        // already compact binary formats, compressing them only inflates build time, and an
        // uncompressed asset is the one form AssetManager.openFd can measure without a full read.
        noCompress += listOf("map", "pt")
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.appcompat:appcompat:1.8.0")
    implementation("androidx.activity:activity-ktx:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    implementation("com.google.android.material:material:1.14.0")
    implementation("com.google.android.gms:play-services-location:21.4.0")
    implementation("org.osmdroid:osmdroid-android:6.1.20")
    // Pulls mapsforge 0.21.0 transitively. Do not force a newer mapsforge: this bridge was
    // compiled against 0.21 and the render-theme APIs changed after it.
    implementation("org.osmdroid:osmdroid-mapsforge:6.1.20")
    
    // PyTorch Android dependencies for ML inference
    implementation("org.pytorch:pytorch_android_lite:2.1.0")
    implementation("org.pytorch:pytorch_android_torchvision_lite:2.1.0")
}
