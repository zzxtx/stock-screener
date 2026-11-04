package com.example.stockscreen

import org.json.JSONArray
import java.net.URLEncoder
import java.util.Collections
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * 沪深主板「缺口 + 放量 + MACD 低位」筛选逻辑。
 *
 * 五条信号条件（同时满足才算命中）：
 *   1 涨幅 2%~5%
 *   2 成交量 > 5 日均量且 <= 2 倍
 *   3 换手率 5%~10%（流通市值 50~200 亿在初筛阶段过滤）
 *   4 2 日前缺口且未回补
 *   5 MACD：DIF 零轴附近或下方，绿柱缩短或刚金叉
 *
 * 与 PC 版 pc/screen.py 的口径完全一致。
 */
object StockScreen {

    // ===== 可配置参数 =====
    const val NMC_MIN_YI = 50.0
    const val NMC_MAX_YI = 200.0
    const val CHG_MIN = 0.02
    const val CHG_MAX = 0.05
    const val VOL_MA_PERIOD = 5
    const val VOL_MULT_MIN = 1.0
    const val VOL_MULT_MAX = 2.0
    const val TURNOVER_MIN = 5.0
    const val TURNOVER_MAX = 10.0
    const val GAP_MIN_AGE = 2
    const val GAP_WINDOW = 60
    const val GAP_MIN_PCT = 0.2
    const val GAP_ALLOW_PARTIAL = false
    const val MACD_FAST = 12
    const val MACD_SLOW = 26
    const val MACD_SIGNAL = 9
    const val DIF_ZERO_BAND_PCT = 0.01
    const val MIN_BARS = 90
    const val K_FETCH_DAYS = 320
    const val THREADS = 4
    /** 边界比较容差：避免 5.00% 这类恰好等于上限时被浮点误差误杀 */
    private const val EPS = 1e-9
    const val POOL_MIN_SCORE = 4

    private val SH_MAIN = listOf("600", "601", "603", "605")
    private val SZ_MAIN = listOf("000", "001", "002")
    private val EXCLUDE_NAME = listOf("ST", "退")
    private val COND_NAMES = listOf("涨幅", "换手率", "量能", "缺口", "MACD")

    private const val KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    private const val LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    private const val COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
    private const val REF = "https://finance.sina.com.cn"

    data class Stock(val code: String, val name: String, val symbol: String,
                     val nmcYi: Double, val nmcYuan: Double)

    data class Row(
        val code: String, val name: String, val baseDate: String,
        val close: Double, val chgPct: Double, val turnoverPct: Double,
        val nmcYi: Double, val volRatio: Double,
        val volWanShou: Double, val volMa5WanShou: Double,
        val gapDate: String, val gapPct: Double, val gapAge: Int, val gapRange: String,
        val macdType: String,
        val dif: Double, val dea: Double, val hist: Double,
        val score: Int, val missing: String
    )

    private data class Bar(val day: String, val open: Double, val high: Double,
                           val low: Double, val close: Double, val volume: Double)

    interface Listener {
        fun onState(state: String)
        fun onProgress(done: Int, total: Int, hits: Int)
        fun onResult(hits: List<Row>, pool: List<Row>, funnel: IntArray, error: String?)
        fun onFinish()
    }

    fun run(asOf: String, listener: Listener) {
        Thread {
            try {
                listener.onState("正在获取沪深 A 股列表…")
                val stamps = fetchStockList { s -> listener.onState(s) }
                listener.onState("初筛剩余 ${stamps.size} 只，开始拉取日 K…")
                if (stamps.isEmpty()) {
                    listener.onResult(emptyList(), emptyList(), IntArray(COND_NAMES.size), null)
                    return@Thread
                }
                val total = stamps.size
                val done = AtomicInteger(0)
                val hitCount = AtomicInteger(0)
                val rows = Collections.synchronizedList(ArrayList<Row>())
                val failFirst = AtomicIntegerArray7()
                val pool = Executors.newFixedThreadPool(THREADS)
                try {
                    val futures = ArrayList<java.util.concurrent.Future<*>>()
                    for (i in 0 until minOf(THREADS, total)) {
                        futures.add(pool.submit {
                            while (true) {
                                val idx = done.getAndIncrement()
                                if (idx >= total) break
                                val row = try { evaluate(stamps[idx], asOf) } catch (e: Exception) { null }
                                if (row != null) {
                                    rows.add(row)
                                    failFirst.inc(firstFailIndex(row))
                                    if (row.score == COND_NAMES.size) hitCount.incrementAndGet()
                                }
                                listener.onProgress(minOf(done.get(), total), total, hitCount.get())
                                Thread.sleep(60)
                            }
                        })
                    }
                    for (f in futures) f.get()
                } finally {
                    pool.shutdown()
                }
                val hits = rows.filter { it.score == COND_NAMES.size }.sortedByDescending { it.chgPct }
                val near = rows.filter { it.score in POOL_MIN_SCORE until COND_NAMES.size }
                    .sortedWith(compareByDescending<Row> { it.score }.thenByDescending { it.chgPct })
                listener.onResult(hits, near, failFirst.prefix(), null)
            } catch (e: Exception) {
                listener.onResult(emptyList(), emptyList(), IntArray(COND_NAMES.size), e.message ?: "unknown error")
            } finally {
                listener.onFinish()
            }
        }.start()
    }

    /** 统计每行「第几条开始不满足」，用于漏斗。 */
    private class AtomicIntegerArray7 {
        private val arr = IntArray(COND_NAMES.size + 1)
        @Synchronized fun inc(i: Int) { arr[i] = arr[i] + 1 }
        /** prefix[i] = 通过了前 i+1 条的只数 */
        @Synchronized fun prefix(): IntArray {
            val out = IntArray(COND_NAMES.size)
            for (i in out.indices) {
                var c = 0
                for (j in (i + 1)..COND_NAMES.size) c += arr[j]
                out[i] = c
            }
            return out
        }
    }

    private fun firstFailIndex(r: Row): Int = COND_NAMES.indexOfFirst { name ->
        when (name) {
            "涨幅" -> r.chgPct >= CHG_MIN * 100 - EPS && r.chgPct <= CHG_MAX * 100 + EPS
            "换手率" -> r.turnoverPct >= TURNOVER_MIN - EPS && r.turnoverPct <= TURNOVER_MAX + EPS
            "量能" -> r.volRatio > VOL_MULT_MIN + EPS && r.volRatio <= VOL_MULT_MAX + EPS
            "缺口" -> r.gapDate.isNotEmpty()
            else -> r.macdType.isNotEmpty()
        }
    }.let { if (it < 0) COND_NAMES.size else it }

    // ===== 单只股票判定 =====

    private fun evaluate(c: Stock, asOf: String): Row? {
        var bars = fetchKline(c.symbol)
        if (bars.isEmpty()) return null

        val floatShares = if (bars.last().close > 0) c.nmcYuan / bars.last().close else 0.0
        bars = bars.filter { it.day <= asOf }
        bars = dropIncompleteToday(bars, asOf)
        if (bars.size < MIN_BARS) return null

        val n = bars.size
        val closes = DoubleArray(n) { bars[it].close }
        val vols = DoubleArray(n) { bars[it].volume }
        if (vols[n - 1] <= 0.0 || closes[n - 2] <= 0.0) return null

        // 1 涨幅
        val chg = (closes[n - 1] - closes[n - 2]) / closes[n - 2]
        val okChg = chg >= CHG_MIN - EPS && chg <= CHG_MAX + EPS

        // 3 换手率
        val turnover = if (floatShares > 0) vols[n - 1] / floatShares * 100.0 else 0.0
        val okTurn = turnover >= TURNOVER_MIN - EPS && turnover <= TURNOVER_MAX + EPS

        // 2 量能
        var avgVol = 0.0
        for (i in n - VOL_MA_PERIOD until n) avgVol += vols[i]
        avgVol /= VOL_MA_PERIOD
        val volRatio = if (avgVol > 0) vols[n - 1] / avgVol else 0.0
        val okVol = volRatio > VOL_MULT_MIN + EPS && volRatio <= VOL_MULT_MAX + EPS

        // 4 缺口
        val gap = findUnfilledGap(bars)

        // 5 MACD
        val macd = checkMacd(closes)
        val (dif, dea, hist) = calcMacd(closes)

        val flags = booleanArrayOf(okChg, okTurn, okVol, gap != null, macd.second.isNotEmpty())
        val score = flags.count { it }
        val missing = COND_NAMES.filterIndexed { i, _ -> !flags[i] }.joinToString("/")

        return Row(
            code = c.code, name = c.name, baseDate = bars[n - 1].day,
            close = round2(closes[n - 1]), chgPct = round2(chg * 100),
            turnoverPct = round2(turnover), nmcYi = c.nmcYi, volRatio = round2(volRatio),
            volWanShou = round2(vols[n - 1] / 1e6), volMa5WanShou = round2(avgVol / 1e6),
            gapDate = gap?.date ?: "", gapPct = gap?.pct ?: 0.0,
            gapAge = gap?.age ?: 0, gapRange = gap?.range ?: "",
            macdType = macd.second,
            dif = round3(dif[n - 1]), dea = round3(dea[n - 1]), hist = round3(hist[n - 1]),
            score = score, missing = missing
        )
    }

    private data class Gap(val date: String, val pct: Double, val age: Int, val range: String)

    private fun findUnfilledGap(bars: List<Bar>): Gap? {
        val n = bars.size
        val latestOk = n - 1 - GAP_MIN_AGE
        val earliest = maxOf(1, n - 1 - GAP_WINDOW)
        if (latestOk < earliest) return null
        for (i in latestOk downTo earliest) {
            val prevHigh = bars[i - 1].high
            val low = bars[i].low
            if (prevHigh <= 0 || low <= prevHigh) continue
            val pct = (low - prevHigh) / prevHigh * 100.0
            if (pct < GAP_MIN_PCT) continue
            var lowestAfter = Double.MAX_VALUE
            for (j in i + 1 until n) if (bars[j].low < lowestAfter) lowestAfter = bars[j].low
            if (lowestAfter == Double.MAX_VALUE) continue
            val limit = if (GAP_ALLOW_PARTIAL) prevHigh else low
            if (lowestAfter <= limit) continue
            return Gap(bars[i].day, round2(pct), n - 1 - i,
                String.format(java.util.Locale.US, "%.2f ~ %.2f", prevHigh, low))
        }
        return null
    }

    private fun dropIncompleteToday(bars: List<Bar>, asOf: String): List<Bar> {
        if (bars.isEmpty()) return bars
        val fmt = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US)
        val now = java.util.Date()
        val today = fmt.format(now).substring(0, 10)
        val hm = fmt.format(now).substring(11)
        if (bars.last().day != today || asOf < today) return bars
        if (hm < "15:05") return bars.dropLast(1)
        return bars
    }

    // ===== 指标 =====

    private fun calcEma(vals: DoubleArray, p: Int): DoubleArray {
        val ema = DoubleArray(vals.size)
        if (vals.isEmpty()) return ema
        ema[0] = vals[0]
        val k = 2.0 / (p + 1)
        for (i in 1 until vals.size) ema[i] = vals[i] * k + ema[i - 1] * (1 - k)
        return ema
    }

    private fun calcMacd(closes: DoubleArray): Triple<DoubleArray, DoubleArray, DoubleArray> {
        val fast = calcEma(closes, MACD_FAST)
        val slow = calcEma(closes, MACD_SLOW)
        val dif = DoubleArray(closes.size) { fast[it] - slow[it] }
        val dea = calcEma(dif, MACD_SIGNAL)
        val hist = DoubleArray(closes.size) { 2.0 * (dif[it] - dea[it]) }
        return Triple(dif, dea, hist)
    }

    /** 返回 (是否满足, 形态描述) */
    private fun checkMacd(closes: DoubleArray): Pair<Boolean, String> {
        val n = closes.size
        if (n < 2) return Pair(false, "")
        val (dif, _, hist) = calcMacd(closes)
        val i = n - 1
        if (dif[i] > closes[i] * DIF_ZERO_BAND_PCT) return Pair(false, "")
        val justCross = hist[i] > 0 && hist[i - 1] <= 0
        val greenShrink = hist[i] < 0 && hist[i] > hist[i - 1]
        return when {
            justCross -> Pair(true, "刚金叉")
            greenShrink -> Pair(true, "绿柱缩短")
            else -> Pair(false, "")
        }
    }

    // ===== 拉取 =====

    private fun fetchStockList(onState: (String) -> Unit): List<Stock> {
        val total = getTotalCount()
        onState("列表总数约 $total 只，正在分页获取…")
        val out = ArrayList<Stock>()
        var page = 1
        while (out.size < total) {
            val url = "$LIST_URL?${qs(mapOf("page" to page, "num" to 100, "sort" to "symbol", "asc" to 1, "node" to "hs_a"))}"
            val text = HttpUtil.get(url, REF)
            val arr = try { JSONArray(text) } catch (e: Exception) { JSONArray() }
            if (arr.length() == 0) break
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val code = o.optString("code")
                val name = o.optString("name")
                val nmc = o.optDouble("nmc", 0.0)   // 万元
                val nmcYi = nmc / 10000.0
                if (isMainBoard(code) && !isExcluded(name) &&
                    nmcYi >= NMC_MIN_YI && nmcYi <= NMC_MAX_YI
                ) {
                    out.add(Stock(code, name, toSymbol(code), round2(nmcYi), nmc * 10000.0))
                }
            }
            if (arr.length() < 100) break
            page++
            Thread.sleep(80)
        }
        return out
    }

    private fun getTotalCount(): Int = try {
        HttpUtil.get("$COUNT_URL?${qs(mapOf("node" to "hs_a"))}", REF).trim().toIntOrNull() ?: 6000
    } catch (e: Exception) {
        6000
    }

    private fun fetchKline(symbol: String): List<Bar> {
        val url = "$KLINE_URL?${qs(mapOf("symbol" to symbol, "scale" to 240, "ma" to "no", "datalen" to K_FETCH_DAYS))}"
        val text = HttpUtil.get(url, null)
        val arr = try { JSONArray(text) } catch (e: Exception) { JSONArray() }
        val bars = ArrayList<Bar>(arr.length())
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val day = o.optString("day").trim().substringBefore(" ")
            val c = o.optDouble("close", Double.NaN)
            if (day.isEmpty() || c.isNaN() || c <= 0) continue
            bars.add(
                Bar(
                    day,
                    o.optDouble("open", c),
                    o.optDouble("high", c),
                    o.optDouble("low", c),
                    c,
                    o.optDouble("volume", 0.0)
                )
            )
        }
        bars.sortBy { it.day }
        return bars
    }

    // ===== 工具 =====

    private fun isMainBoard(code: String): Boolean {
        if (!Regex("""^\d{6}$""").matches(code)) return false
        return SH_MAIN.any { code.startsWith(it) } || SZ_MAIN.any { code.startsWith(it) }
    }

    private fun isExcluded(name: String): Boolean {
        val up = name.uppercase().replace(" ", "")
        return EXCLUDE_NAME.any { up.contains(it) }
    }

    private fun toSymbol(code: String): String =
        if (code.firstOrNull()?.let { it in '5'..'6' } == true) "sh$code" else "sz$code"

    private fun qs(obj: Map<String, Any?>): String =
        obj.entries.joinToString("&") {
            URLEncoder.encode(it.key, "UTF-8") + "=" + URLEncoder.encode(it.value?.toString() ?: "", "UTF-8")
        }

    private fun round2(v: Double): Double = Math.round(v * 100.0) / 100.0
    private fun round3(v: Double): Double = Math.round(v * 1000.0) / 1000.0
}
