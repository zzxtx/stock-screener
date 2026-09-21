package com.example.stockscreen

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.File

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var pendingAsOf: String? = null

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            val asOf = pendingAsOf
            pendingAsOf = null
            // 无论是否授权都启动（未授权则无通知，但服务仍会执行）
            startScreenService(asOf ?: return@registerForActivityResult)
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.setSupportZoom(false)
        webView.webViewClient = WebViewClient()
        webView.addJavascriptInterface(NativeBridge(), "NativeBridge")
        webView.loadUrl("file:///android_asset/index.html")
        setContentView(webView)
    }

    private fun startScreenService(asOf: String) {
        val intent = Intent(this, ScreenService::class.java)
        intent.putExtra(ScreenService.EXTRA_AS_OF, asOf)
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private inner class NativeBridge {

        @JavascriptInterface
        fun fetch(url: String, referer: String, callbackId: Int) {
            Thread {
                try {
                    val ref = if (referer.isNullOrBlank()) null else referer
                    val text = HttpUtil.get(url, ref)
                    postOk(callbackId, text)
                } catch (e: Exception) {
                    postFail(callbackId, e.message ?: "network error")
                }
            }.start()
        }

        @JavascriptInterface
        fun startScreen(asOf: String) {
            // Android 13+ 需要通知权限
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                pendingAsOf = asOf
                permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                startScreenService(asOf)
            }
        }

        @JavascriptInterface
        fun readResult(): String {
            return try {
                val file = File(filesDir, ScreenService.RESULT_FILE)
                if (!file.exists()) "{}" else file.readText()
            } catch (e: Exception) {
                "{}"
            }
        }

        @JavascriptInterface
        fun clearResult() {
            try {
                File(filesDir, ScreenService.RESULT_FILE).delete()
            } catch (e: Exception) {
                // 忽略
            }
        }
    }

    private fun postOk(id: Int, text: String) {
        webView.post {
            val quoted = JSONObject.quote(text)
            webView.evaluateJavascript("window.__nativeDone($id, $quoted);", null)
        }
    }

    private fun postFail(id: Int, msg: String) {
        webView.post {
            val quoted = JSONObject.quote(msg)
            webView.evaluateJavascript("window.__nativeFail($id, $quoted);", null)
        }
    }
}