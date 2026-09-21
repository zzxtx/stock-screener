package com.example.stockscreen

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class ScreenService : Service() {

    companion object {
        const val ACTION_START = "com.example.stockscreen.action.START"
        const val EXTRA_AS_OF = "asOf"
        const val CHANNEL_PROGRESS = "screen_progress"
        const val CHANNEL_DONE = "screen_done"
        const val NOTIF_PROGRESS_ID = 1001
        const val NOTIF_DONE_ID = 1002
        const val RESULT_FILE = "result.json"
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val asOf = intent?.getStringExtra(EXTRA_AS_OF) ?: today()
        startForegroundCompat()
        updateProgress("准备中…", "0/0", 0)

        StockScreen.run(asOf, object : StockScreen.Listener {
            override fun onState(state: String) {
                updateProgress(state, null, 0)
            }

            override fun onProgress(done: Int, total: Int, hits: Int) {
                val text = "$done/$total"
                val pct = if (total == 0) 0 else (done * 100 / total)
                updateProgress("筛选中… 已命中 $hits 只", text, pct)
            }

            override fun onFillResult(results: List<StockScreen.StockResult>, error: String?) {
                writeResult(results, error)
            }

            override fun onFinish() {
                onScreenFinish()
            }
        })
        return START_NOT_STICKY
    }

    private fun createChannels() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val prog = NotificationChannel(
            CHANNEL_PROGRESS, "筛选中", NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "后台筛选进度"
            setShowBadge(false)
        }
        val done = NotificationChannel(
            CHANNEL_DONE, "筛选完成", NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "筛选结果通知"
        }
        nm.createNotificationChannel(prog)
        nm.createNotificationChannel(done)
    }

    private fun startForegroundCompat() {
        val pending = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notif = NotificationCompat.Builder(this, CHANNEL_PROGRESS)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("股票筛选中")
            .setContentText("正在准备…")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pending)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_PROGRESS_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_PROGRESS_ID, notif)
        }
    }

    private fun updateProgress(state: String, sub: String?, pct: Int) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val pending = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val b = NotificationCompat.Builder(this, CHANNEL_PROGRESS)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("股票筛选中")
            .setContentText(state)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pending)
        if (sub != null) {
            b.setContentText(sub).setStyle(NotificationCompat.BigTextStyle().bigText(state))
            b.setProgress(100, pct, false)
        }
        nm.notify(NOTIF_PROGRESS_ID, b.build())
    }

    private fun writeResult(results: List<StockScreen.StockResult>, error: String?) {
        try {
            val arr = JSONArray()
            for (r in results) {
                val o = JSONObject()
                o.put("code", r.code)
                o.put("name", r.name)
                o.put("baseDate", r.baseDate)
                o.put("baseClose", r.baseClose)
                o.put("nmcYi", r.nmcYi)
                arr.put(o)
            }
            val root = JSONObject()
            root.put("asOf", today())
            root.put("error", error ?: JSONObject.NULL)
            root.put("results", arr)
            File(filesDir, RESULT_FILE).writeText(root.toString())
        } catch (e: Exception) {
            // 忽略写入失败
        }
    }

    private fun onScreenFinish() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val result = readResultSummary()
        val pending = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val b = NotificationCompat.Builder(this, CHANNEL_DONE)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("筛选完成")
            .setContentText("点击查看结果（$result）")
            .setStyle(NotificationCompat.BigTextStyle().bigText("筛选完成：$result"))
            .setAutoCancel(true)
            .setContentIntent(pending)
        nm.notify(NOTIF_DONE_ID, b.build())

        // 收起进度通知
        nm.cancel(NOTIF_PROGRESS_ID)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun readResultSummary(): String {
        return try {
            val file = File(filesDir, RESULT_FILE)
            if (!file.exists()) return "无结果"
            val root = JSONObject(file.readText())
            val err = if (root.isNull("error")) null else root.optString("error")
            if (err != null) "出错：$err"
            else "命中 ${root.getJSONArray("results").length()} 只"
        } catch (e: Exception) {
            "无结果"
        }
    }

    private fun today(): String {
        return java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US)
            .format(java.util.Date())
    }
}