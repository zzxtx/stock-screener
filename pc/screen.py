# -*- coding: utf-8 -*-
"""
沪深主板「涨停 + 跳空缺口 + 筹码收敛」选股脚本（数据源：新浪财经公开接口）

筛选条件（全部满足才算命中）：
  0. 市场：沪深主板（沪 600/601/603/605，深 000/001/002），剔除 ST / *ST / 退市
  1. 股价：3 元 < 收盘价 < 25 元
  2. 涨停：涨停日 T 恰好是 2 个交易日前，且 T 是「首板」（T-1 不是涨停）、
     「非一字板」（当日盘中打开过，最低价明显低于收盘价）
  3. 缺口：T+1 跳空（缺口日本身不是涨停），且到最新交易日为止，
     缺口上方（含缺口当根）只有 2 根 K 线

  换句话说：这是给「涨停 → 次日跳空 → 第三日进场」这条节奏定的定时信号。
  涨停日 T、缺口日 T+1、运行当日 T+2（即今天进场），次日 T+3 找机会离场。
  4. 筹码：当日筹码集中度小于上一日（筹码在收敛）

筹码集中度采用通达信 CYQ 口径：
    集中度 = (P95 - P5) / (P95 + P5) × 100
其中 P5 / P95 是筹码分布累计 5% 和 95% 分位对应的价格。**数值越小表示筹码越集中。**

仅使用 Python 标准库，无第三方依赖。
运行：python screen.py [基准日期 YYYY-MM-DD]

本脚本只做数据筛选，不构成任何投资建议。
"""

import argparse
import concurrent.futures
import csv
import datetime
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

# ================== 可配置参数 ==================

# --- 市场范围 ---
SH_MAIN = ("600", "601", "603", "605")
SZ_MAIN = ("000", "001", "002")
EXCLUDE_NAME_KEYWORDS = ("ST", "退")

# --- 条件 1：股价 ---
PRICE_MIN = 3.0           # 不含
PRICE_MAX = 25.0          # 不含

# --- 条件 2 / 3：涨停 + 缺口位置 ---
LIMIT_UP_PCT = 0.098      # 主板 10% 口径，按涨幅 >= 9.8% 近似
# 缺口上方（含缺口当根）的 K 线根数。设 2 表示：
#   · 缺口落在倒数第 2 根 K 线
#   · 涨停日就是它前一根（倒数第 3 根），即涨停恰好 2 个交易日前
BARS_ABOVE_GAP = 2
# 首板：涨停日 T 的前一交易日 T-1 不能再是涨停（这是 T 为「首板」的定义）。
# 取代原来的「非连板」说法。
FIRST_BOARD = True
# 非一字板：T 当日不能开盘即封死、全天不打开。
# 判定口径 = 当日最低价与收盘价之间至少有 YIZI_TOL_PCT 的落差；
# 一字板满足 low == close，会被这条剔除。
NOT_YIZI_BAN = True
YIZI_TOL_PCT = 0.2        # 最低价比收盘价低不足这个百分比 → 视为一字板
# 缺口日（T+1）本身不能是涨停 —— 它属于缺口条件，不在「首板」范围内：
# 缺口若由一根涨停板造出来，图形上就是连板，而且一字板根本买不进。
GAP_DAY_NOT_LIMIT_UP = True
GAP_MIN_PCT = 0.2         # 缺口幅度下限 %
GAP_ALLOW_PARTIAL = False # False=完全未回补；True=允许部分回补

# --- 条件 4：筹码集中度（CYQ） ---
CYQ_WINDOW = 90           # 筹码迭代窗口（交易日）
CYQ_BINS = 120            # 价格网格档数
CYQ_DECAY = 1.0           # 历史筹码衰减强度（1.0 = 按换手率全额衰减）
SCR_SHRINK = True         # True=要求今日集中度 < 昨日（收敛）

# --- 其它 ---
MIN_BARS = 120            # 最少需要的 K 线根数
K_FETCH_DAYS = 320
SLEEP_SEC = 0.12
DEFAULT_WORKERS = 6
MAX_RETRY = 3
LIST_PAGE_SIZE = 100
DROP_TODAY_BEFORE = "15:05"
EPS = 1e-9

# ================== 接口地址 ==================

LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
COUNT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/Market_Center.getHQNodeStockCount")
KLINE_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/"
             "CN_MarketData.getKLineData")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
KLINE_HEADERS = {"User-Agent": UA}

# 四条信号条件（顺序即漏斗顺序）
CONDITIONS = [
    ("股价", "3 元 < 股价 < 25 元"),
    ("涨停", "首板·非一字板·2 日前"),
    ("缺口", "次日跳空·缺口日不涨停·上方仅 2 根"),
    ("筹码", "筹码集中度小于上一日"),
]
COND_NAMES = [c[0] for c in CONDITIONS]


# ================== 网络层 ==================

def http_get_json(url, headers=None, retries=MAX_RETRY):
    headers = headers or HEADERS
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace").strip())
        except Exception as e:          # noqa: BLE001
            last_err = e
            time.sleep(0.8)
    raise RuntimeError("请求失败 %s: %s" % (url, last_err))


def get_total_count():
    params = urllib.parse.urlencode({"node": "hs_a"})
    try:
        raw = http_get_json("%s?%s" % (COUNT_URL, params))
    except Exception:                   # noqa: BLE001
        return 6000
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return 6000
    if isinstance(raw, (int, float)):
        return int(raw)
    return 6000


def fetch_stock_list(verbose=True):
    """分页拉取沪深 A 股全市场快照（nmc 单位为万元）。"""
    total = get_total_count()
    if verbose:
        print("沪深 A 股总数约：%d 只" % total)
    stocks, page = [], 1
    while len(stocks) < total:
        params = urllib.parse.urlencode({
            "page": page, "num": LIST_PAGE_SIZE, "sort": "symbol",
            "asc": 1, "node": "hs_a",
        })
        try:
            data = http_get_json("%s?%s" % (LIST_URL, params))
        except Exception as e:          # noqa: BLE001
            if verbose:
                print("列表第 %d 页失败：%s" % (page, e))
            break
        if not data:
            break
        stocks.extend(data)
        if len(data) < LIST_PAGE_SIZE:
            break
        page += 1
        time.sleep(0.15)
    if verbose:
        print("实际拉取到 %d 只" % len(stocks))
    return stocks


def fetch_kline(symbol):
    """日 K（升序），volume 单位为股。"""
    params = urllib.parse.urlencode({
        "symbol": symbol, "scale": 240, "ma": "no", "datalen": K_FETCH_DAYS,
    })
    data = http_get_json("%s?%s" % (KLINE_URL, params), headers=KLINE_HEADERS)
    if not isinstance(data, list) or not data:
        return []
    bars = []
    for bar in data:
        try:
            day = str(bar.get("day", "")).strip().split(" ")[0]
            o = float(bar.get("open", 0))
            h = float(bar.get("high", 0))
            l = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
            v = float(bar.get("volume", 0))
        except (TypeError, ValueError):
            continue
        if not day or c <= 0:
            continue
        bars.append({"day": day, "open": o, "high": h, "low": l,
                     "close": c, "volume": v})
    bars.sort(key=lambda b: b["day"])
    return bars


# ================== 工具函数 ==================

def is_main_board(code):
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(SH_MAIN) or code.startswith(SZ_MAIN)


def is_excluded_name(name):
    up = (name or "").upper().replace(" ", "")
    return any(k in up for k in EXCLUDE_NAME_KEYWORDS)


def to_sina_symbol(code):
    return ("sh" + code) if code[0] in ("5", "6") else ("sz" + code)


def drop_incomplete_today(bars, as_of):
    """收盘前运行时不把当日未完成的 K 线计入。"""
    if not bars:
        return bars
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if bars[-1]["day"] != today or as_of < today:
        return bars
    if now.strftime("%H:%M") < DROP_TODAY_BEFORE:
        return bars[:-1]
    return bars


# ================== 条件 2：涨停 ==================

def is_limit_up(bars, i):
    """第 i 根是否涨停（主板 10% 口径）。"""
    if i < 1:
        return False
    prev = bars[i - 1]["close"]
    if prev <= 0:
        return False
    return (bars[i]["close"] - prev) / prev >= LIMIT_UP_PCT


def is_yizi_ban(bars, i):
    """
    第 i 根是否一字板：开盘即封死涨停、全天不打开。

    判定口径看「最低价相对收盘价的落差」—— 一字板满足 low == close；
    T 字板（盘中打开过）low 会明显低于 close，算非一字板。
    """
    b = bars[i]
    if b["close"] <= 0:
        return False
    return (b["close"] - b["low"]) / b["close"] * 100.0 < YIZI_TOL_PCT


def limit_up_index(bars):
    """
    涨停日被缺口位置钉死：缺口上方只有 BARS_ABOVE_GAP 根 K 线，
    所以缺口在倒数第 BARS_ABOVE_GAP 根，涨停就在它前一根。

    返回涨停日下标；不满足「该位置是涨停」「首板」「非一字板」时返回 None。
    """
    n = len(bars)
    gap_i = n - BARS_ABOVE_GAP          # 缺口应出现的下标
    t = gap_i - 1                        # 涨停日在缺口前一根
    if t < 1:
        return None
    if not is_limit_up(bars, t):
        return None
    if FIRST_BOARD and is_limit_up(bars, t - 1):
        return None                      # T-1 也涨停 → 不是首板
    if NOT_YIZI_BAN and is_yizi_ban(bars, t):
        return None                      # 一字板
    return t


# ================== 条件 3：缺口 ==================

def find_gap_after_limit_up(bars, t):
    """
    缺口只认「涨停次日」，且位置固定：当天起算到最新一根，缺口上方恰好
    BARS_ABOVE_GAP 根 K 线（含缺口当根）。因此缺口下标 = t + 1。

    向上跳空缺口：当日最低价 > 前一日最高价，缺口区间 = [前一日最高, 当日最低]。
    未回补：缺口之后所有 K 线的最低价都没进入该区间。

    另外要求缺口日本身不是涨停 —— 否则那根涨停板的上一日（即 T）必然是涨停，
    图形上就是连板；一字板也根本买不进。
    """
    n = len(bars)
    i = t + 1
    if i < 1 or i >= n:
        return None
    if GAP_DAY_NOT_LIMIT_UP and is_limit_up(bars, i):
        return None
    prev_high = bars[i - 1]["high"]
    low = bars[i]["low"]
    if prev_high <= 0 or low <= prev_high:
        return None
    gap_pct = (low - prev_high) / prev_high * 100.0
    if gap_pct < GAP_MIN_PCT:
        return None
    after = bars[i + 1:]
    lowest_after = min((b["low"] for b in after), default=None)
    if lowest_after is not None:
        limit = prev_high if GAP_ALLOW_PARTIAL else low
        if lowest_after <= limit:
            return None
    prev_close = bars[i - 1]["close"]
    day_chg = ((bars[i]["close"] - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    return {
        "gap_index": i,
        "gap_date": bars[i]["day"],
        "gap_pct": round(gap_pct, 2),
        "gap_day_chg": round(day_chg, 2),
        "gap_age": n - 1 - i,
        "bars_above": n - i,
        "gap_range": "%.2f ~ %.2f" % (prev_high, low),
    }


# ================== 条件 4：筹码集中度（CYQ） ==================

def _triangle_weights(bins, step, pmin, lo, hi, peak):
    """
    把一段筹码按三角形分布铺在 [lo, hi] 上，峰在 peak。
    返回 [(档位下标, 权重)]，权重之和为 1。
    """
    if hi <= lo:
        idx = min(max(int((lo - pmin) / step), 0), bins - 1)
        return [(idx, 1.0)]
    i0 = min(max(int((lo - pmin) / step), 0), bins - 1)
    i1 = min(max(int((hi - pmin) / step), 0), bins - 1)
    if i1 <= i0:
        return [(i0, 1.0)]
    peak = min(max(peak, lo), hi)
    left = max(peak - lo, 1e-9)
    right = max(hi - peak, 1e-9)
    out = []
    total = 0.0
    for j in range(i0, i1 + 1):
        price = pmin + (j + 0.5) * step
        if price <= peak:
            w = (price - lo) / left
        else:
            w = (hi - price) / right
        w = max(w, 0.0)
        if w > 0:
            out.append((j, w))
            total += w
    if total <= 0:
        return [(i0, 1.0)]
    return [(j, w / total) for j, w in out]


def _scr(chips, pmin, step):
    """
    集中度 = (P95 - P5) / (P95 + P5) × 100
    返回 (集中度, P5, P95)，数值越小越集中。

    分位数在档内线性插值，否则价格网格较粗时（例如 0.15 元一档）
    连续几天的 P5/P95 会被钉死在同一档，导致集中度完全不变。
    """
    total = sum(chips)
    if total <= 0:
        return None
    t5 = total * 0.05
    t95 = total * 0.95
    acc = 0.0
    p5 = p95 = None
    for j, c in enumerate(chips):
        if c <= 0:
            continue
        lo = pmin + j * step
        nxt = acc + c
        if p5 is None and nxt >= t5:
            p5 = lo + (t5 - acc) / c * step
        if nxt >= t95:
            p95 = lo + (t95 - acc) / c * step
            break
        acc = nxt
    if p5 is None or p95 is None:
        return None
    if p5 + p95 <= 0:
        return None
    return (p95 - p5) / (p95 + p5) * 100.0, p5, p95


def chip_concentration(bars, float_shares, window=CYQ_WINDOW, bins=CYQ_BINS):
    """
    计算最后两个交易日的筹码集中度，返回 (今日, 昨日, 今日P5, 今日P95)。
    某日无法计算时对应位置为 None。

    模型：三角形分布 + 换手衰减
      · 每日旧筹码按 (1 - 换手率 × CYQ_DECAY) 衰减
      · 当日换手部分按三角形分布铺在 [low, high] 上，峰在均价
    """
    n = len(bars)
    if float_shares <= 0 or n < 2:
        return None, None, None, None
    start = max(0, n - window)
    seg = bars[start:]
    pmin = min(b["low"] for b in seg)
    pmax = max(b["high"] for b in seg)
    if pmax <= pmin:
        return None, None, None, None
    step = (pmax - pmin) / bins

    chips = [0.0] * bins
    prev = None
    cur = None
    prev_p5 = prev_p95 = None
    cur_p5 = cur_p95 = None
    for idx in range(start, n):
        b = bars[idx]
        tr = b["volume"] / float_shares
        tr = min(max(tr, 0.0), 0.95)
        if not any(chips):
            chips = [0.0] * bins
        else:
            keep = max(0.0, 1.0 - tr * CYQ_DECAY)
            if keep < 1.0:
                chips = [c * keep for c in chips]
        if tr > 0:
            lo, hi = b["low"], b["high"]
            peak = (b["high"] + b["low"] + b["close"]) / 3.0
            add = tr
            for j, w in _triangle_weights(bins, step, pmin, lo, hi, peak):
                chips[j] += add * w
        res = _scr(chips, pmin, step)
        prev, prev_p5, prev_p95 = cur, cur_p5, cur_p95
        if res:
            cur, cur_p5, cur_p95 = res
        else:
            cur = cur_p5 = cur_p95 = None
    return cur, prev, cur_p5, cur_p95


# ================== 主流程 ==================

def build_candidates(stocks):
    """主板 + 非 ST。股价与其余条件都在拿到 K 线后再判。"""
    cands = []
    for s in stocks:
        code = str(s.get("code", "")).strip()
        if not is_main_board(code):
            continue
        name = str(s.get("name", "")).strip()
        if is_excluded_name(name):
            continue
        try:
            nmc = float(s.get("nmc", 0) or 0)      # 万元
        except (TypeError, ValueError):
            continue
        cands.append({
            "code": code,
            "name": name,
            "symbol": to_sina_symbol(code),
            "nmc_yi": round(nmc / 10000.0, 2) if nmc else 0.0,
            "nmc_yuan": nmc * 10000.0,
        })
    return cands


def evaluate(c, as_of, stats, lock):
    """逐条判定四个条件，返回结果 dict 或 None（数据不足）。"""
    def bump(key):
        with lock:
            stats[key] = stats.get(key, 0) + 1

    try:
        bars = fetch_kline(c["symbol"])
    except Exception:                    # noqa: BLE001
        bump("K线失败")
        return None
    if not bars:
        bump("无K线")
        return None

    last_close_raw = bars[-1]["close"]
    float_shares = c["nmc_yuan"] / last_close_raw if last_close_raw > 0 else 0.0

    bars = [b for b in bars if b["day"] <= as_of]
    bars = drop_incomplete_today(bars, as_of)
    if len(bars) < MIN_BARS:
        bump("K线不足")
        return None

    n = len(bars)
    close = bars[-1]["close"]
    if bars[-1]["volume"] <= 0 or close <= 0:
        bump("停牌")
        return None

    # ---- 条件 1：股价 ----
    ok_price = (PRICE_MIN + EPS) < close < (PRICE_MAX - EPS)

    # ---- 条件 2：涨停 ----
    t = limit_up_index(bars)
    ok_limit = t is not None

    # ---- 条件 3：缺口 ----
    gap = find_gap_after_limit_up(bars, t) if ok_limit else None
    ok_gap = gap is not None

    # ---- 条件 4：筹码集中度 ----
    scr_today, scr_prev, p5, p95 = chip_concentration(bars, float_shares)
    ok_chip = False
    if scr_today is not None and scr_prev is not None:
        ok_chip = (scr_today < scr_prev) if SCR_SHRINK else (scr_today > scr_prev)

    flags = {
        "股价": ok_price,
        "涨停": ok_limit,
        "缺口": ok_gap,
        "筹码": ok_chip,
    }
    score = sum(1 for v in flags.values() if v)
    missing = [k for k in COND_NAMES if not flags[k]]
    if score == len(COND_NAMES):
        bump("命中")

    chg = (close - bars[-2]["close"]) / bars[-2]["close"] * 100.0 if bars[-2]["close"] > 0 else 0.0
    return {
        "code": c["code"],
        "name": c["name"],
        "base_date": bars[-1]["day"],
        "close": round(close, 2),
        "chg_pct": round(chg, 2),
        "nmc_yi": c["nmc_yi"],
        "limit_date": bars[t]["day"] if t is not None else "",
        "limit_age": (n - 1 - t) if t is not None else "",
        "limit_body_pct": (round((bars[t]["close"] - bars[t]["low"]) / bars[t]["close"] * 100.0, 2)
                           if t is not None and bars[t]["close"] > 0 else ""),
        "gap_date": gap["gap_date"] if gap else "",
        "gap_pct": gap["gap_pct"] if gap else "",
        "gap_day_chg": gap["gap_day_chg"] if gap else "",
        "gap_age": gap["gap_age"] if gap else "",
        "bars_above": gap["bars_above"] if gap else "",
        "gap_range": gap["gap_range"] if gap else "",
        "scr_today": round(scr_today, 2) if scr_today is not None else "",
        "scr_prev": round(scr_prev, 2) if scr_prev is not None else "",
        "scr_delta": round(scr_today - scr_prev, 2)
                     if (scr_today is not None and scr_prev is not None) else "",
        "scr_p5": round(p5, 2) if p5 else "",
        "scr_p95": round(p95, 2) if p95 else "",
        "score": score,
        "missing": "/".join(missing),
        "flags": flags,
    }


CSV_FIELDS = [
    ("code", "代码"), ("name", "名称"), ("base_date", "基准日期"),
    ("close", "收盘价"), ("chg_pct", "当日涨幅%"), ("nmc_yi", "流通市值(亿)"),
    ("limit_date", "涨停日"), ("limit_age", "涨停距今(日)"),
    ("limit_body_pct", "涨停日实体%(低/收)"),
    ("gap_date", "缺口日"), ("gap_pct", "缺口幅度%"),
    ("gap_day_chg", "缺口日涨幅%"), ("gap_age", "缺口距今(日)"),
    ("bars_above", "缺口上方K线数"), ("gap_range", "缺口区间"),
    ("scr_today", "今日集中度"), ("scr_prev", "昨日集中度"),
    ("scr_delta", "集中度变化"), ("scr_p5", "成本P5"), ("scr_p95", "成本P95"),
]

SKIP_KEYS = ("K线失败", "无K线", "K线不足", "停牌", "计算异常")


def print_funnel(rows, total):
    entered = len(rows)
    print("\n" + "-" * 60)
    print("筛选漏斗（候选 %d 只，可计算 %d 只）" % (total, entered))
    print("-" * 60)
    if not entered:
        print("  无可计算样本")
        return
    alive = rows
    for name, desc in CONDITIONS:
        alive = [r for r in alive if r["flags"][name]]
        pct = len(alive) / entered * 100
        filled = int(round(pct / 4.0))
        bar = "#" * filled + "." * (25 - filled)
        print("  [%s] %-24s %4d 只 %5.1f%%  %s"
              % ("OK" if alive else "--", desc, len(alive), pct, bar))
    print("-" * 60)
    print("  四条全部满足：%d 只" % len(alive))


def write_csv(rows, path, fields):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([label for _, label in fields])
        for r in rows:
            w.writerow([r.get(k, "") for k, _ in fields])


def default_out_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "output")


def ask_date():
    while True:
        raw = input("请输入筛选基准日期（YYYY-MM-DD，直接回车=最近交易日）：").strip()
        if not raw:
            return datetime.datetime.now().strftime("%Y-%m-%d")
        try:
            return datetime.datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print("日期格式错误，请按 YYYY-MM-DD 重新输入。")


def main():
    ap = argparse.ArgumentParser(description="涨停 + 跳空缺口 + 筹码收敛 选股")
    ap.add_argument("date", nargs="?", help="基准日期 YYYY-MM-DD，缺省为最近交易日")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="仅测试前 N 只")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    as_of = args.date or ask_date()
    try:
        datetime.datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError:
        print("日期格式错误：%s" % as_of)
        return 1

    out_dir = args.out_dir or default_out_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "%s_%s" % (as_of.replace("-", ""), ts)

    print("基准日期：%s（取该日或之前最近一个交易日）" % as_of)
    stocks = fetch_stock_list()
    cands = build_candidates(stocks)
    print("主板 + 非 ST 初筛后：%d 只" % len(cands))
    if args.limit:
        cands = cands[:args.limit]
        print("（测试模式，仅计算前 %d 只）" % len(cands))
    if not cands:
        print("没有候选股票，退出。")
        return 0

    total = len(cands)
    stats, lock = {}, threading.Lock()
    done = [0]
    rows = []
    t0 = time.time()

    def work(c):
        try:
            r = evaluate(c, as_of, stats, lock)
        except Exception as e:           # noqa: BLE001
            with lock:
                stats["计算异常"] = stats.get("计算异常", 0) + 1
            print("  ! %s %s 计算异常: %s" % (c["code"], c["name"], e))
            r = None
        with lock:
            done[0] += 1
            if done[0] % 200 == 0 or done[0] == total:
                print("  进度 %d/%d  用时 %.0fs" % (done[0], total, time.time() - t0))
        time.sleep(SLEEP_SEC)
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for r in pool.map(work, cands):
            if r:
                rows.append(r)

    hits = [r for r in rows if r["score"] == len(COND_NAMES)]
    hits.sort(key=lambda r: (-r["chg_pct"], r["code"]))

    hit_path = os.path.join(out_dir, "命中_%s.csv" % tag)
    write_csv(hits, hit_path, CSV_FIELDS)

    print_funnel(rows, total)
    skipped = [(k, stats.get(k, 0)) for k in SKIP_KEYS if stats.get(k)]
    if skipped:
        print("  跳过：" + "，".join("%s %d 只" % kv for kv in skipped))

    print("\n" + "=" * 60)
    if hits:
        print("命中 %d 只（四条条件全部满足）：" % len(hits))
        for r in hits:
            print("  %s %-6s 收 %.2f 涨 %+.2f%% | 涨停 %s(%s日前) | 缺口 %s(+%.2f%%,%s日前) "
                  "| 集中度 %s → %s"
                  % (r["code"], r["name"], r["close"], r["chg_pct"],
                     r["limit_date"], r["limit_age"], r["gap_date"], r["gap_pct"],
                     r["gap_age"], r["scr_prev"], r["scr_today"]))
    else:
        print("命中 0 只 —— 四条条件同时成立的概率较低，属正常结果")

    print("\n用时 %.0fs" % (time.time() - t0))
    print("结果文件：%s" % hit_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已手动终止")
        sys.exit(1)
