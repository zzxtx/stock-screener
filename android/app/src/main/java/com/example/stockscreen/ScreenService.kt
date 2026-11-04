package com.example.stockscreen

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
                val pct = if (total == 0) 0 else (done * 100 / total)
                updateProgress("筛选中… 已命中 $hits 只", "$done/$total", pct)
            }

            override fun onResult(hits: List<StockScreen.Row>, pool: List<StockScreen.Row>,
                                  funnel: IntArray, error: String?) {
                writeResult(asOf, hits, pool, funnel, error)
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

    private fun pendingIntent(): PendingIntent = PendingIntent.getActivity(
        this, 0,
        Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
    )

    private fun startForegroundCompat() {
        val notif = NotificationCompat.Builder(this, CHANNEL_PROGRESS)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("股票筛选中")
            .setContentText("正在准备…")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pendingIntent())
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_PROGRESS_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_PROGRESS_ID, notif)
        }
    }

    private fun updateProgress(state: String, sub: String?, pct: Int) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val b = NotificationCompat.Builder(this, CHANNEL_PROGRESS)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("股票筛选中")
            .setContentText(state)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pendingIntent())
        if (sub != null) {
            b.setContentText(sub).setStyle(NotificationCompat.BigTextStyle().bigText(state))
            b.setProgress(100, pct, false)
        }
        nm.notify(NOTIF_PROGRESS_ID, b.build())
    }

    private fun rowToJson(r: StockScreen.Row): JSONObject = JSONObject().apply {
        put("code", r.code)
        put("name", r.name)
        put("baseDate", r.baseDate)
        put("close", r.close)
        put("chgPct", r.chgPct)
        put("turnoverPct", r.turnoverPct)
        put("nmcYi", r.nmcYi)
        put("volRatio", r.volRatio)
        put("volWanShou", r.volWanShou)
        put("volMa5WanShou", r.volMa5WanShou)
        put("gapDate", r.gapDate)
        put("gapPct", r.gapPct)
        put("gapAge", r.gapAge)
        put("gapRange", r.gapRange)
        put("macdType", r.macdType)
        put("dif", r.dif)
        put("dea", r.dea)
        put("hist", r.hist)
        put("score", r.score)
        put("missing", r.missing)
    }

    private fun writeResult(asOf: String, hits: List<StockScreen.Row>, pool: List<StockScreen.Row>,
                            funnel: IntArray, error: String?) {
        try {
            val hitArr = JSONArray()
            for (r in hits) hitArr.put(rowToJson(r))
            val poolArr = JSONArray()
            for (r in pool) poolArr.put(rowToJson(r))
            val fn = JSONArray()
            for (v in funnel) fn.put(v)
            val root = JSONObject()
            root.put("asOf", asOf)
            root.put("error", error ?: JSONObject.NULL)
            root.put("funnel", fn)
            root.put("hits", hitArr)
            root.put("pool", poolArr)
            File(filesDir, RESULT_FILE).writeText(root.toString())
        } catch (e: Exception) {
            // 忽略写入失败
        }
    }

    private fun onScreenFinish() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val summary = readResultSummary()
        val b = NotificationCompat.Builder(this, CHANNEL_DONE)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("筛选完成")
            .setContentText(summary)
            .setStyle(NotificationCompat.BigTextStyle().bigText(summary))
            .setAutoCancel(true)
            .setContentIntent(pendingIntent())
        nm.notify(NOTIF_DONE_ID, b.build())

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
            if (err != null) {
                "出错：$err"
            } else {
                val h = root.optJSONArray("hits")?.length() ?: 0
                val p = root.optJSONArray("pool")?.length() ?: 0
                "命中 $h 只，观察池 $p 只"
            }
        } catch (e: Exception) {
            "无结果"
        }
    }

    private fun today(): String =
        java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(java.util.Date())
}
