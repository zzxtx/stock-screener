package com.example.stockscreen

import org.json.JSONArray
import java.net.URLEncoder
import java.util.Collections
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max
import kotlin.math.min

/**
 * 沪深主板「涨停 + 次日跳空缺口 + 筹码收敛」筛选逻辑。
 *
 * 四条信号条件（同时满足才算命中）：
 *   1 股价 3 < 收盘 < 25
 *   2 涨停日 T 恰好 2 个交易日前，且非连板（T-1 不是涨停）
 *   3 T+1 跳空，且到最新一根为止缺口上方（含缺口当根）只有 2 根 K 线
 *
 * 本质是「涨停 -> 次日跳空 -> 第三日进场，次日离场」的定时信号：
 * 涨停 T、缺口 T+1、运行当日 T+2、离场 T+3。
 *   4 当日筹码集中度 < 上一日（筹码收敛）
 *
 * 与 PC 版 pc/screen.py 的口径完全一致，包含 CYQ 筹码分布模型。
 */
object StockScreen {

    // ===== 可配置参数 =====
    const val PRICE_MIN = 3.0
    const val PRICE_MAX = 25.0
    const val LIMIT_UP_PCT = 0.098
    /** 缺口上方（含缺口当根）的 K 线根数：2 表示缺口在倒数第 2 根，涨停在其前一根 */
    const val BARS_ABOVE_GAP = 2
    const val NO_CONSECUTIVE = true
    const val GAP_MIN_PCT = 0.2
    const val GAP_ALLOW_PARTIAL = false
    const val CYQ_WINDOW = 90
    const val CYQ_BINS = 120
    const val CYQ_DECAY = 1.0
    const val SCR_SHRINK = true
    const val MIN_BARS = 120
    const val K_FETCH_DAYS = 320
    const val THREADS = 4
    private const val EPS = 1e-9

    private val SH_MAIN = listOf("600", "601", "603", "605")
    private val SZ_MAIN = listOf("000", "001", "002")
    private val EXCLUDE_NAME = listOf("ST", "退")
    private val COND_NAMES = listOf("股价", "涨停", "缺口", "筹码")

    private const val KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    private const val LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    private const val COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
    private const val REF = "https://finance.sina.com.cn"

    data class Stock(val code: String, val name: String, val symbol: String,
                     val nmcYi: Double, val nmcYuan: Double)

    data class Row(
        val code: String, val name: String, val baseDate: String,
        val close: Double, val chgPct: Double, val nmcYi: Double,
        val limitDate: String, val limitAge: Int,
        val gapDate: String, val gapPct: Double, val gapAge: Int,
        val barsAbove: Int, val gapRange: String,
        val scrToday: Double, val scrPrev: Double, val scrDelta: Double,
        val scrP5: Double, val scrP95: Double,
        val score: Int, val missing: String
    )

    private data class Bar(val day: String, val open: Double, val high: Double,
                           val low: Double, val close: Double, val volume: Double)

    private data class Gap(val index: Int, val date: String, val pct: Double,
                           val age: Int, val barsAbove: Int, val range: String)

    interface Listener {
        fun onState(state: String)
        fun onProgress(done: Int, total: Int, hits: Int)
        fun onResult(hits: List<Row>, funnel: IntArray, entered: Int, error: String?)
        fun onFinish()
    }

    fun run(asOf: String, listener: Listener) {
        Thread {
            try {
                listener.onState("正在获取沪深 A 股列表…")
                val stocks = fetchStockList { s -> listener.onState(s) }
                listener.onState("初筛剩余 ${stocks.size} 只，开始拉取日 K 并计算筹码…")
                if (stocks.isEmpty()) {
                    listener.onResult(emptyList(), IntArray(COND_NAMES.size), 0, null)
                    return@Thread
                }
                val total = stocks.size
                val done = AtomicInteger(0)
                val hitCount = AtomicInteger(0)
                val rows = Collections.synchronizedList(ArrayList<Row>())
                val failFirst = FailCounter()
                val pool = Executors.newFixedThreadPool(THREADS)
                try {
                    val futures = ArrayList<java.util.concurrent.Future<*>>()
                    for (i in 0 until minOf(THREADS, total)) {
                        futures.add(pool.submit {
                            while (true) {
                                val idx = done.getAndIncrement()
                                if (idx >= total) break
                                val row = try { evaluate(stocks[idx], asOf) } catch (e: Exception) { null }
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
                val hits = rows.filter { it.score == COND_NAMES.size }
                    .sortedByDescending { it.chgPct }
                listener.onResult(hits, failFirst.prefix(), rows.size, null)
            } catch (e: Exception) {
                listener.onResult(emptyList(), IntArray(COND_NAMES.size), 0,
                    e.message ?: "unknown error")
            } finally {
                listener.onFinish()
            }
        }.start()
    }

    /** 统计每行「第几条开始不满足」，用于漏斗。 */
    private class FailCounter {
        private val arr = IntArray(COND_NAMES.size + 1)
        @Synchronized fun inc(i: Int) { arr[i] = arr[i] + 1 }
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

    private fun firstFailIndex(r: Row): Int {
        val i = when {
            !(r.close > PRICE_MIN + EPS && r.close < PRICE_MAX - EPS) -> 0
            r.limitDate.isEmpty() -> 1
            r.gapDate.isEmpty() -> 2
            !(if (SCR_SHRINK) r.scrToday < r.scrPrev else r.scrToday > r.scrPrev) -> 3
            else -> -1
        }
        return if (i < 0) COND_NAMES.size else i
    }

    // ===== 单只股票判定 =====

    private fun evaluate(c: Stock, asOf: String): Row? {
        var bars = fetchKline(c.symbol)
        if (bars.isEmpty()) return null

        val floatShares = if (bars.last().close > 0) c.nmcYuan / bars.last().close else 0.0
        bars = bars.filter { it.day <= asOf }
        bars = dropIncompleteToday(bars, asOf)
        if (bars.size < MIN_BARS) return null

        val n = bars.size
        val close = bars[n - 1].close
        if (bars[n - 1].volume <= 0.0 || close <= 0.0) return null

        // 1 股价
        val okPrice = close > PRICE_MIN + EPS && close < PRICE_MAX - EPS

        // 2 涨停
        val t = limitUpIndex(bars)
        // 3 缺口（仅次日）
        val gap = if (t >= 0) findGapAfterLimitUp(bars, t) else null

        // 4 筹码
        val chip = chipConcentration(bars, floatShares)
        val scrToday = chip[0]
        val scrPrev = chip[1]
        val okChip = if (scrToday.isNaN() || scrPrev.isNaN()) false
        else if (SCR_SHRINK) scrToday < scrPrev else scrToday > scrPrev

        val flags = booleanArrayOf(okPrice, t >= 0, gap != null, okChip)
        val score = flags.count { it }
        val missing = COND_NAMES.filterIndexed { i, _ -> !flags[i] }.joinToString("/")

        val prevClose = bars[n - 2].close
        val chg = if (prevClose > 0) (close - prevClose) / prevClose * 100.0 else 0.0

        return Row(
            code = c.code, name = c.name, baseDate = bars[n - 1].day,
            close = round2(close), chgPct = round2(chg), nmcYi = c.nmcYi,
            limitDate = if (t >= 0) bars[t].day else "",
            limitAge = if (t >= 0) n - 1 - t else 0,
            gapDate = gap?.date ?: "", gapPct = gap?.pct ?: 0.0,
            gapAge = gap?.age ?: 0, barsAbove = gap?.barsAbove ?: 0,
            gapRange = gap?.range ?: "",
            scrToday = if (scrToday.isNaN()) 0.0 else round2(scrToday),
            scrPrev = if (scrPrev.isNaN()) 0.0 else round2(scrPrev),
            scrDelta = if (scrToday.isNaN() || scrPrev.isNaN()) 0.0 else round2(scrToday - scrPrev),
            scrP5 = if (chip[2].isNaN()) 0.0 else round2(chip[2]),
            scrP95 = if (chip[3].isNaN()) 0.0 else round2(chip[3]),
            score = score, missing = missing
        )
    }

    // ===== 涨停 =====

    private fun isLimitUp(bars: List<Bar>, i: Int): Boolean {
        if (i < 1) return false
        val prev = bars[i - 1].close
        if (prev <= 0) return false
        return (bars[i].close - prev) / prev >= LIMIT_UP_PCT
    }

    /**
     * 涨停日被缺口位置钉死：缺口在倒数第 BARS_ABOVE_GAP 根，涨停在它前一根。
     * 返回涨停日下标，不满足返回 -1。
     */
    private fun limitUpIndex(bars: List<Bar>): Int {
        val n = bars.size
        val gapIdx = n - BARS_ABOVE_GAP
        val t = gapIdx - 1
        if (t < 1) return -1
        if (!isLimitUp(bars, t)) return -1
        if (NO_CONSECUTIVE && isLimitUp(bars, t - 1)) return -1
        return t
    }

    // ===== 缺口 =====

    /** 缺口只认涨停次日，位置固定为 t+1。 */
    private fun findGapAfterLimitUp(bars: List<Bar>, t: Int): Gap? {
        val n = bars.size
        val i = t + 1
        if (i < 1 || i >= n) return null
        val prevHigh = bars[i - 1].high
        val low = bars[i].low
        if (prevHigh <= 0 || low <= prevHigh) return null
        val pct = (low - prevHigh) / prevHigh * 100.0
        if (pct < GAP_MIN_PCT) return null
        var lowestAfter = Double.MAX_VALUE
        for (j in i + 1 until n) if (bars[j].low < lowestAfter) lowestAfter = bars[j].low
        if (lowestAfter != Double.MAX_VALUE) {
            val limit = if (GAP_ALLOW_PARTIAL) prevHigh else low
            if (lowestAfter <= limit) return null
        }
        return Gap(i, bars[i].day, round2(pct), n - 1 - i, n - i,
            String.format(java.util.Locale.US, "%.2f ~ %.2f", prevHigh, low))
    }

    // ===== 筹码集中度（CYQ） =====

    /** 返回 [今日集中度, 昨日集中度, 今日P5, 今日P95]，无法计算时为 NaN。 */
    private fun chipConcentration(bars: List<Bar>, floatShares: Double): DoubleArray {
        val nan = doubleArrayOf(Double.NaN, Double.NaN, Double.NaN, Double.NaN)
        val n = bars.size
        if (floatShares <= 0 || n < 2) return nan
        val start = max(0, n - CYQ_WINDOW)
        var pmin = Double.MAX_VALUE
        var pmax = -Double.MAX_VALUE
        for (k in start until n) {
            if (bars[k].low < pmin) pmin = bars[k].low
            if (bars[k].high > pmax) pmax = bars[k].high
        }
        if (pmax <= pmin) return nan
        val bins = CYQ_BINS
        val step = (pmax - pmin) / bins
        val chips = DoubleArray(bins)
        var initialized = false
        var prev = Double.NaN
        var cur = Double.NaN
        var curP5 = Double.NaN
        var curP95 = Double.NaN

        for (idx in start until n) {
            val b = bars[idx]
            var tr = b.volume / floatShares
            tr = min(max(tr, 0.0), 0.95)
            if (initialized) {
                val keep = max(0.0, 1.0 - tr * CYQ_DECAY)
                if (keep < 1.0) for (j in chips.indices) chips[j] *= keep
            }
            if (tr > 0) {
                val peak = (b.high + b.low + b.close) / 3.0
                addTriangle(chips, bins, step, pmin, b.low, b.high, peak, tr)
                initialized = true
            }
            prev = cur
            val r = scrOf(chips, pmin, step)
            if (r == null) {
                cur = Double.NaN
                curP5 = Double.NaN
                curP95 = Double.NaN
            } else {
                cur = r[0]; curP5 = r[1]; curP95 = r[2]
            }
        }
        return doubleArrayOf(cur, prev, curP5, curP95)
    }

    private fun addTriangle(chips: DoubleArray, bins: Int, step: Double, pmin: Double,
                            lo: Double, hi: Double, peakRaw: Double, amount: Double) {
        if (hi <= lo) {
            val idx = min(max(((lo - pmin) / step).toInt(), 0), bins - 1)
            chips[idx] += amount
            return
        }
        val i0 = min(max(((lo - pmin) / step).toInt(), 0), bins - 1)
        val i1 = min(max(((hi - pmin) / step).toInt(), 0), bins - 1)
        if (i1 <= i0) {
            chips[i0] += amount
            return
        }
        val peak = min(max(peakRaw, lo), hi)
        val left = max(peak - lo, 1e-9)
        val right = max(hi - peak, 1e-9)
        var total = 0.0
        val ws = DoubleArray(i1 - i0 + 1)
        for (j in i0..i1) {
            val price = pmin + (j + 0.5) * step
            val w = if (price <= peak) (price - lo) / left else (hi - price) / right
            ws[j - i0] = max(w, 0.0)
            total += ws[j - i0]
        }
        if (total <= 0) {
            chips[i0] += amount
            return
        }
        for (j in i0..i1) chips[j] += amount * ws[j - i0] / total
    }

    /** 集中度 = (P95-P5)/(P95+P5)*100，分位数在档内线性插值。 */
    private fun scrOf(chips: DoubleArray, pmin: Double, step: Double): DoubleArray? {
        var total = 0.0
        for (c in chips) total += c
        if (total <= 0) return null
        val t5 = total * 0.05
        val t95 = total * 0.95
        var acc = 0.0
        var p5 = Double.NaN
        var p95 = Double.NaN
        for (j in chips.indices) {
            val c = chips[j]
            if (c <= 0) continue
            val lo = pmin + j * step
            val nxt = acc + c
            if (p5.isNaN() && nxt >= t5) p5 = lo + (t5 - acc) / c * step
            if (nxt >= t95) {
                p95 = lo + (t95 - acc) / c * step
                break
            }
            acc = nxt
        }
        if (p5.isNaN() || p95.isNaN() || p5 + p95 <= 0) return null
        return doubleArrayOf((p95 - p5) / (p95 + p5) * 100.0, p5, p95)
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
                if (isMainBoard(code) && !isExcluded(name) && nmc > 0) {
                    out.add(Stock(code, name, toSymbol(code), round2(nmc / 10000.0), nmc * 10000.0))
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
                Bar(day, o.optDouble("open", c), o.optDouble("high", c),
                    o.optDouble("low", c), c, o.optDouble("volume", 0.0))
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
}
