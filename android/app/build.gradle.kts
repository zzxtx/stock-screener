plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    // Kotlin 包名保持 com.example.stockscreen（源码目录结构不变）
    namespace = "com.example.stockscreen"
    compileSdk = 34

    defaultConfig {
        // applicationId 才是设备上的唯一包名。
        // 旧版「股票筛选」用的是 com.example.stockscreen，这里必须不同，
        // 否则装新包会把旧包覆盖掉。改成 gapscreen 后两个 App 可共存。
        applicationId = "com.example.gapscreen"
        minSdk = 21
        targetSdk = 34
        versionCode = 2
        versionName = "1.1"
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
