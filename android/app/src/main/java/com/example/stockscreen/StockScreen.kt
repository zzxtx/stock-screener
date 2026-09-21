package com.example.stockscreen

import org.json.JSONArray
import java.net.URLEncoder
import java.util.Collections
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

object StockScreen {

    const val PRICE_MIN = 3.0
    const val PRICE_MAX = 30.0
    const val NMC_MIN = 300000.0   // 万元
    const val NMC_MAX = 2000000.0  // 万元
    const val K_DAYS = 60
    const val K_FETCH_DAYS = 500
    const val WINDOW = 20
    const val TARGET = 2

    private val SH_MAIN = listOf("600", "601", "603", "605")
    private val SZ_MAIN = listOf("000", "001", "002")

    private const val KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    private const val LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    private const val COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
    private const val REF = "https://finance.sina.com.cn"

    data class Stock(val code: String, val name: String, val symbol: String, val nmcYi: Double)
    data class StockResult(val code: String, val name: String, val baseDate: String, val baseClose: Double, val nmcYi: Double)

    private data class Bar(val day: String, val close: Double)

    interface Listener {
        fun onState(state: String)
        fun onProgress(done: Int, total: Int, hits: Int)
        fun onFillResult(results: List<StockResult>, error: String?)
        fun onFinish()
    }

    fun run(asOf: String, listener: Listener) {
        Thread {
            val results = Collections.synchronizedList(ArrayList<StockResult>())
            try {
                listener.onState("正在获取沪深 A 股列表…")
                val stocks = fetchStockList { s -> listener.onState(s) }
                listener.onState("初筛剩余 ${stocks.size} 只，开始拉取日 K 并计算信号…")

                val total = stocks.size
                if (total == 0) {
                    listener.onFillResult(emptyList(), null)
                } else {
                    val done = AtomicInteger(0)
                    val pool = Executors.newFixedThreadPool(4)
                    try {
                        val futures = ArrayList<java.util.concurrent.Future<*>>()
                        for (i in 0 until minOf(4, total)) {
                            futures.add(pool.submit { worker(stocks, total, done, asOf, listener, results) })
                        }
                        for (f in futures) f.get()
                    } finally {
                        pool.shutdown()
                    }
                    listener.onFillResult(results.sortedBy { it.code }, null)
                }
            } catch (e: Exception) {
                listener.onFillResult(emptyList(), e.message ?: "unknown error")
            } finally {
                listener.onFinish()
            }
        }.start()
    }

    private fun worker(
        candidates: List<Stock>,
        total: Int,
        done: AtomicInteger,
        asOf: String,
        listener: Listener,
        results: MutableList<StockResult>
    ) {
        while (true) {
            val idx = done.getAndIncrement()
            if (idx >= total) break
            val c = candidates[idx]
            try {
                val bars = fetchKline(c.symbol)
                val sliced = bars.filter { it.day <= asOf }
                if (sliced.size >= K_DAYS) {
                    val closes = DoubleArray(sliced.size) { sliced[it].close }
                    val baseClose = closes[closes.size - 1]
                    val baseDate = sliced[sliced.size - 1].day
                    if (baseClose >= PRICE_MIN && baseClose <= PRICE_MAX) {
                        if (checkSignal(closes) && hasLimitUp(closes, WINDOW)) {
                            results.add(StockResult(c.code, c.name, baseDate, baseClose, c.nmcYi))
                        }
                    }
                }
            } catch (e: Exception) {
                // 单只失败跳过
            }
            listener.onProgress(done.get(), total, results.size)
            Thread.sleep(60)
        }
    }

    // ===== 网络拉取 =====

    private fun fetchStockList(onState: (String) -> Unit): List<Stock> {
        val total = getTotalCount()
        onState("列表总数约 $total 只，正在分页获取…")
        val stocks = ArrayList<Stock>()
        var page = 1
        while (stocks.size < total) {
            val url = "$LIST_URL?${qs(mapOf("page" to page, "num" to 100, "sort" to "symbol", "asc" to 1, "node" to "hs_a"))}"
            val text = HttpUtil.get(url, REF)
            val arr = try { JSONArray(text) } catch (e: Exception) { JSONArray() }
            if (arr.length() == 0) break
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val code = o.optString("code")
                val name = o.optString("name")
                val nmc = o.optDouble("nmc", 0.0)
                if (isMainBoard(code) && !name.uppercase().contains("ST") &&
                    nmc > 0 && nmc >= NMC_MIN && nmc <= NMC_MAX
                ) {
                    stocks.add(Stock(code, name, toSymbol(code), nmc / 10000.0))
                }
            }
            if (arr.length() < 100) break
            page++
            Thread.sleep(80)
        }
        return stocks
    }

    private fun getTotalCount(): Int {
        return try {
            HttpUtil.get("$COUNT_URL?${qs(mapOf("node" to "hs_a"))}", REF).trim().toIntOrNull() ?: 6000
        } catch (e: Exception) {
            6000
        }
    }

    private fun fetchKline(symbol: String): List<Bar> {
        val url = "$KLINE_URL?${qs(mapOf("symbol" to symbol, "scale" to 240, "ma" to "no", "datalen" to K_FETCH_DAYS))}"
        val text = HttpUtil.get(url, null)
        val arr = try { JSONArray(text) } catch (e: Exception) { JSONArray() }
        val bars = ArrayList<Bar>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val day = o.optString("day").trim().substringBefore(" ")
            val close = o.optDouble("close", Double.NaN)
            if (day.isNotEmpty() && !close.isNaN()) bars.add(Bar(day, close))
        }
        return bars
    }

    // ===== 工具 =====

    private fun isMainBoard(code: String): Boolean {
        if (!Regex("""^\d{6}$""").matches(code)) return false
        return SH_MAIN.any { code.startsWith(it) } || SZ_MAIN.any { code.startsWith(it) }
    }

    private fun toSymbol(code: String): String {
        return if (code.firstOrNull()?.let { it in '5'..'6' } == true) "sh$code" else "sz$code"
    }

    private fun qs(obj: Map<String, Any?>): String {
        return obj.entries.joinToString("&") {
            URLEncoder.encode(it.key, "UTF-8") + "=" + URLEncoder.encode(it.value?.toString() ?: "", "UTF-8")
        }
    }

    // ===== 技术指标 =====

    private fun calcMA(vals: DoubleArray, p: Int): DoubleArray {
        val ma = DoubleArray(vals.size) { Double.NaN }
        var sum = 0.0
        for (i in vals.indices) {
            sum += vals[i]
            if (i >= p) sum -= vals[i - p]
            if (i >= p - 1) ma[i] = sum / p
        }
        return ma
    }

    private fun calcEMA(vals: DoubleArray, p: Int): DoubleArray {
        val ema = DoubleArray(vals.size)
        if (vals.isEmpty()) return ema
        ema[0] = vals[0]
        val k = 2.0 / (p + 1)
        for (i in 1 until vals.size) ema[i] = vals[i] * k + ema[i - 1] * (1 - k)
        return ema
    }

    private fun macdGoldenLatest(closes: DoubleArray): Boolean {
        val n = closes.size
        if (n < 2) return false
        val e12 = calcEMA(closes, 12)
        val e26 = calcEMA(closes, 26)
        val dif = DoubleArray(n)
        for (i in 0 until n) dif[i] = e12[i] - e26[i]
        val dea = calcEMA(dif, 9)
        val i = n - 1
        return dif[i] > dea[i] && dif[i - 1] <= dea[i - 1]
    }

    private fun countGoldenCross(ma5: DoubleArray, ma10: DoubleArray, window: Int): Pair<Int, Boolean> {
        val n = ma5.size
        if (n < window + 1) return Pair(0, false)
        val start = n - window
        var count = 0
        var latest = false
        for (i in maxOf(1, start) until n) {
            if (ma5[i].isNaN() || ma10[i].isNaN() || ma5[i - 1].isNaN() || ma10[i - 1].isNaN()) continue
            if (ma5[i] > ma10[i] && ma5[i - 1] <= ma10[i - 1]) {
                count++
                if (i == n - 1) latest = true
            }
        }
        return Pair(count, latest)
    }

    private fun checkSignal(closes: DoubleArray): Boolean {
        if (closes.size < K_DAYS) return false
        val ma5 = calcMA(closes, 5)
        val ma10 = calcMA(closes, 10)
        val (count, latest) = countGoldenCross(ma5, ma10, WINDOW)
        if (!(latest && count == TARGET)) return false
        return macdGoldenLatest(closes)
    }

    private fun hasLimitUp(closes: DoubleArray, window: Int): Boolean {
        val n = closes.size
        if (n < 2) return false
        val start = maxOf(1, n - window)
        for (i in start until n) {
            if (closes[i - 1] <= 0) continue
            if ((closes[i] - closes[i - 1]) / closes[i - 1] >= 0.098) return true
        }
        return false
    }
}