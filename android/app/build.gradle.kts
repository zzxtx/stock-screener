plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    // Kotlin 包名保持 com.example.stockscreen（源码目录结构不变）
    namespace = "com.example.stockscreen"
    compileSdk = 34

    defaultConfig {
        // applicationId 是设备上的唯一包名。
        // 旧版「股票筛选」= com.example.stockscreen，「缺口放量选股」= com.example.gapscreen，
        // 这里用 limitupgap，三个 App 可以同时装在一台手机上，互不覆盖。
        applicationId = "com.example.limitupgap"
        minSdk = 21
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }

    kotlinOptions {
        jvmTarget = "1.8"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.core:core-ktx:1.13.1")
}
