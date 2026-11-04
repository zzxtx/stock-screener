# -*- coding: utf-8 -*-
"""
沪深主板「缺口 + 放量 + MACD 低位」选股脚本（数据源：新浪财经公开接口）

筛选条件（全部为「与」关系，同时满足才算命中）：
  0. 市场：沪深主板（沪 600/601/603/605，深 000/001/002），剔除 ST / *ST / 退市股
  1. 涨幅：当日 2% ~ 5%
  2. 成交量：高于 5 日均量，且不超过 5 日均量的 2 倍
  3. 换手率：5% ~ 10%；流通市值：50 亿 ~ 200 亿
  4. 缺口：2 个交易日前已出现向上跳空缺口，且至今未回补
  5. MACD：DIF 在零轴附近或下方；绿柱缩短 或 当日刚金叉；排除零轴上方高位金叉

除「命中」列表外，脚本还会输出「观察池」—— 满足 4/5 条条件的个股，
并标注差的是哪一条，方便条件过严时人工复核。

仅使用 Python 标准库，无第三方依赖。
运行：python screen.py [基准日期 YYYY-MM-DD] [--workers N] [--limit N] [--out 路径]

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
SH_MAIN = ("600", "601", "603", "605")   # 沪市主板
SZ_MAIN = ("000", "001", "002")          # 深市主板（含原中小板）
EXCLUDE_NAME_KEYWORDS = ("ST", "退")      # 名称含这些字样的剔除

# --- 条件 1：当日涨幅 ---
CHG_MIN = 0.02            # 2%
CHG_MAX = 0.05            # 5%

# --- 条件 2：成交量（相对 5 日均量） ---
VOL_MA_PERIOD = 5         # 均量周期
VOL_MULT_MIN = 1.0        # 需 > 1.0 倍均量（严格大于）
VOL_MULT_MAX = 2.0        # 需 <= 2.0 倍均量

# --- 条件 3：换手率 与 流通市值 ---
TURNOVER_MIN = 5.0        # 换手率下限 %
TURNOVER_MAX = 10.0       # 换手率上限 %
NMC_MIN_YI = 50.0         # 流通市值下限（亿元）
NMC_MAX_YI = 200.0        # 流通市值上限（亿元）

# --- 条件 4：缺口 ---
GAP_MIN_AGE = 2           # 缺口至少已存在 N 个交易日（对应「2 日前」）
GAP_WINDOW = 60           # 只在最近 N 个交易日内寻找缺口（原条件未设上限，按 60 日近似）
GAP_MIN_PCT = 0.2         # 缺口幅度（相对前一日最高价）下限 %，过滤噪声
GAP_ALLOW_PARTIAL = False # False=要求完全未回补；True=允许部分回补但未补满

# --- 条件 5：MACD（标准 12 / 26 / 9） ---
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
DIF_ZERO_BAND_PCT = 0.01  # DIF 允许高出零轴的最大幅度（占收盘价比例），1% 内视为「零轴附近」

# --- 其它 ---
MIN_BARS = 90             # 计算 MACD / 均量所需的最少 K 线根数
K_FETCH_DAYS = 320        # 每次拉取的日 K 根数（足够覆盖历史回溯）
PRICE_MIN = 0.0           # 股价下限（元），0 表示不限制
PRICE_MAX = 100000.0      # 股价上限（元），不限制
SLEEP_SEC = 0.12          # 单只 K 线请求后的间隔（秒）
DEFAULT_WORKERS = 6       # 并发线程数
MAX_RETRY = 3             # 单次请求最大重试次数
LIST_PAGE_SIZE = 100      # 列表接口每页数量
POOL_MIN_SCORE = 4        # 观察池门槛：至少满足 N 条（设成条件总数 = 关闭观察池）
DROP_TODAY_BEFORE = "15:05"  # 收盘前运行时不把当日未完成 K 线计入
EPS = 1e-9                # 边界比较容差，避免 5.00% 这类恰好等于上限时被浮点误差误杀

# --loose 预设：实测（2026-10 十个交易日）命中从「10 日 1 只」提升到「日均 1.2 只」
LOOSE_PRESET = {
    "TURNOVER_MIN": 3.0,
    "TURNOVER_MAX": 15.0,
    "GAP_ALLOW_PARTIAL": True,
}

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

# 五条信号条件（顺序即漏斗顺序）
CONDITIONS = [
    ("涨幅", "当日涨幅 2%~5%"),
    ("换手率", "换手率 5%~10%"),
    ("量能", "成交量 1~2 倍 5 日均量"),
    ("缺口", "2 日前缺口且未回补"),
    ("MACD", "MACD 低位：绿柱缩短 / 刚金叉"),
]
COND_NAMES = [c[0] for c in CONDITIONS]


# ================== 网络层 ==================

def http_get_json(url, headers=None, retries=MAX_RETRY):
    """GET 并解析 JSON，失败自动重试。"""
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
    """分页拉取沪深 A 股全市场快照（含 nmc 流通市值，单位万元）。"""
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
    """拉取日 K（升序），返回 [{day, open, high, low, close, volume}]，volume 单位：股。"""
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


def calc_ma(values, period):
    """简单移动平均，前 period-1 个为 None。"""
    ma = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            ma[i] = s / period
    return ma


def calc_ema(values, period):
    """指数移动平均，以首值为种子（与通达信一致）。"""
    ema = [None] * len(values)
    if not values:
        return ema
    k = 2.0 / (period + 1)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def calc_macd(closes):
    """返回 (DIF, DEA, MACD柱)。MACD柱 = 2 * (DIF - DEA)，与通达信口径一致。"""
    fast = calc_ema(closes, MACD_FAST)
    slow = calc_ema(closes, MACD_SLOW)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = calc_ema(dif, MACD_SIGNAL)
    hist = [2.0 * (d - s) for d, s in zip(dif, dea)]
    return dif, dea, hist


def drop_incomplete_today(bars, as_of):
    """
    若基准日就是今天、且当前尚未收盘，去掉今天这根未完成的 K 线，
    避免盘中运行时成交量 / 涨幅失真。
    """
    if not bars:
        return bars
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if bars[-1]["day"] != today or as_of < today:
        return bars
    if now.strftime("%H:%M") < DROP_TODAY_BEFORE:
        return bars[:-1]
    return bars


# ================== 各条件判定 ==================

def check_change(chg):
    """条件 1：当日涨幅 2% ~ 5%（含边界，带浮点容差）。"""
    return CHG_MIN - EPS <= chg <= CHG_MAX + EPS


def check_volume(vols):
    """
    条件 2：当日成交量 > 5 日均量，且 <= 2 倍 5 日均量。
    均量按含当日的前 5 根 K 线计算（通达信 MA(V,5) 口径）。
    """
    n = len(vols)
    if n < VOL_MA_PERIOD:
        return False, 0.0, 0.0
    avg = sum(vols[n - VOL_MA_PERIOD:n]) / VOL_MA_PERIOD
    if avg <= 0:
        return False, 0.0, avg
    ratio = vols[n - 1] / avg
    return (ratio > VOL_MULT_MIN + EPS) and (ratio <= VOL_MULT_MAX + EPS), ratio, avg


def check_turnover(vol_today, float_shares):
    """条件 3：换手率 5% ~ 10%（含边界，带浮点容差）。float_shares 单位：股。"""
    if float_shares <= 0:
        return False, 0.0
    tr = vol_today / float_shares * 100.0
    return TURNOVER_MIN - EPS <= tr <= TURNOVER_MAX + EPS, tr


def find_unfilled_gap(bars):
    """
    条件 4：2 个交易日前已出现向上跳空缺口，且至今未回补。

    向上跳空缺口：当日最低价 > 前一日最高价，缺口区间 = [前一日最高, 当日最低]。
    未回补：缺口出现之后所有 K 线的最低价都没进入该区间。
    返回最近的一个符合条件的缺口；没有则返回 None。
    """
    n = len(bars)
    latest_ok_idx = n - 1 - GAP_MIN_AGE          # 缺口最晚可出现的位置
    earliest_idx = max(1, n - 1 - GAP_WINDOW)     # 搜索窗口起点
    if latest_ok_idx < earliest_idx:
        return None
    for i in range(latest_ok_idx, earliest_idx - 1, -1):
        prev_high = bars[i - 1]["high"]
        low = bars[i]["low"]
        if prev_high <= 0 or low <= prev_high:
            continue
        gap_pct = (low - prev_high) / prev_high * 100.0
        if gap_pct < GAP_MIN_PCT:
            continue
        after = bars[i + 1:]
        if not after:
            continue
        lowest_after = min(b["low"] for b in after)
        # 完全未回补：后续最低价始终高于缺口上沿
        # 部分回补容忍：后续最低价仍高于缺口下沿
        limit = prev_high if GAP_ALLOW_PARTIAL else low
        if lowest_after <= limit:
            continue
        return {
            "gap_date": bars[i]["day"],
            "gap_pct": round(gap_pct, 2),
            "gap_age": n - 1 - i,
            "gap_range": "%.2f ~ %.2f" % (prev_high, low),
            "gap_filled_pct": round(
                max(0.0, min(100.0, (low - lowest_after) / (low - prev_high) * 100.0)), 0),
        }
    return None


def check_macd(closes):
    """
    条件 5：MACD 低位形态。
      a) DIF 在零轴附近或下方：DIF <= 收盘价 * DIF_ZERO_BAND_PCT
         这一条同时排除了「零轴上方高位金叉」。
      b) 绿柱缩短（MACD 柱为负但比前一日收缩）或 当日刚金叉（柱由 <=0 转为 >0）
    返回 (是否满足, 形态描述)。
    """
    n = len(closes)
    if n < 2:
        return False, ""
    dif, dea, hist = calc_macd(closes)
    i = n - 1
    if dif[i] > closes[i] * DIF_ZERO_BAND_PCT:
        return False, ""
    just_cross = hist[i] > 0 and hist[i - 1] <= 0
    green_shrink = hist[i] < 0 and hist[i] > hist[i - 1]
    if just_cross:
        return True, "刚金叉"
    if green_shrink:
        return True, "绿柱缩短"
    return False, ""


# ================== 主流程 ==================

def build_candidates(stocks):
    """按「主板 + 非 ST + 流通市值 50~200 亿」做初筛。"""
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
        nmc_yi = nmc / 10000.0
        if not (NMC_MIN_YI <= nmc_yi <= NMC_MAX_YI):
            continue
        cands.append({
            "code": code,
            "name": name,
            "symbol": to_sina_symbol(code),
            "nmc_yi": round(nmc_yi, 2),
            "nmc_yuan": nmc * 10000.0,
        })
    return cands


def evaluate(c, as_of, stats, lock):
    """
    对单只股票逐条判定全部 6 个条件（不短路，以便统计与观察池）。
    返回结果 dict，或 None（数据不足 / 无法计算）。
    """
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

    # 用接口最新一根 K 线的收盘价推算流通股本（nmc 与最新价同为当前快照）
    last_close_raw = bars[-1]["close"]
    float_shares = c["nmc_yuan"] / last_close_raw if last_close_raw > 0 else 0.0

    bars = [b for b in bars if b["day"] <= as_of]
    bars = drop_incomplete_today(bars, as_of)
    if len(bars) < MIN_BARS:
        bump("K线不足")
        return None

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    n = len(bars)
    if vols[-1] <= 0 or closes[-2] <= 0:
        bump("停牌")
        return None

    # ---- 条件 1：涨幅 ----
    chg = (closes[-1] - closes[-2]) / closes[-2]
    ok_chg = check_change(chg)

    # ---- 条件 3a：换手率 ----
    ok_turn, turnover = check_turnover(vols[-1], float_shares)

    # ---- 条件 2：成交量 ----
    ok_vol, vol_ratio, avg_vol = check_volume(vols)

    # ---- 条件 4：缺口 ----
    gap = find_unfilled_gap(bars)
    ok_gap = gap is not None

    # ---- 条件 5：MACD ----
    ok_macd, macd_type = check_macd(closes)
    dif, dea, hist = calc_macd(closes)

    flags = {
        "涨幅": ok_chg,
        "换手率": ok_turn,
        "量能": ok_vol,
        "缺口": ok_gap,
        "MACD": ok_macd,
    }
    score = sum(1 for v in flags.values() if v)
    missing = [k for k in COND_NAMES if not flags[k]]

    if score == len(COND_NAMES):
        bump("命中")

    r = {
        "code": c["code"],
        "name": c["name"],
        "base_date": bars[-1]["day"],
        "close": round(closes[-1], 2),
        "chg_pct": round(chg * 100, 2),
        "turnover_pct": round(turnover, 2),
        "nmc_yi": c["nmc_yi"],
        "vol_ratio": round(vol_ratio, 2),
        "vol_wan_shou": round(vols[-1] / 1e6, 2),
        "vol_ma5_wan_shou": round(avg_vol / 1e6, 2),
        "gap_date": gap["gap_date"] if gap else "",
        "gap_pct": gap["gap_pct"] if gap else "",
        "gap_age": gap["gap_age"] if gap else "",
        "gap_range": gap["gap_range"] if gap else "",
        "macd_type": macd_type,
        "dif": round(dif[-1], 3),
        "dea": round(dea[-1], 3),
        "hist": round(hist[-1], 3),
        "score": score,
        "missing": "/".join(missing),
        "flags": flags,
    }
    return r


CSV_FIELDS = [
    ("code", "代码"), ("name", "名称"), ("base_date", "基准日期"),
    ("close", "收盘价"), ("chg_pct", "当日涨幅%"), ("turnover_pct", "换手率%"),
    ("nmc_yi", "流通市值(亿)"), ("vol_ratio", "量比(V/MA5V)"),
    ("vol_wan_shou", "成交量(万手)"), ("vol_ma5_wan_shou", "5日均量(万手)"),
    ("gap_date", "缺口日"), ("gap_pct", "缺口幅度%"), ("gap_age", "缺口天数"),
    ("gap_range", "缺口区间"),
    ("macd_type", "MACD形态"), ("dif", "DIF"), ("dea", "DEA"), ("hist", "MACD柱"),
]

POOL_FIELDS = CSV_FIELDS + [("score", "满足条数"), ("missing", "未满足条件")]

SKIP_KEYS = ("K线失败", "无K线", "K线不足", "停牌", "计算异常")


def print_funnel(rows, total):
    """按条件顺序打印累积通过率。"""
    entered = len(rows)
    print("\n" + "-" * 58)
    print("筛选漏斗（候选 %d 只，可计算 %d 只）" % (total, entered))
    print("-" * 58)
    if not entered:
        print("  无可计算样本")
        return
    alive = rows
    for name, desc in CONDITIONS:
        alive = [r for r in alive if r["flags"][name]]
        pct = len(alive) / entered * 100
        filled = int(round(pct / 4.0))
        bar = "#" * filled + "." * (25 - filled)
        print("  [%s] %-26s %4d 只 %5.1f%%  %s"
              % ("OK" if alive else "--", desc, len(alive), pct, bar))
    print("-" * 58)
    print("  五条全部满足：%d 只" % len(alive))


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
    ap = argparse.ArgumentParser(description="沪深主板缺口放量选股")
    ap.add_argument("date", nargs="?", help="基准日期 YYYY-MM-DD，缺省为最近交易日")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发线程数")
    ap.add_argument("--limit", type=int, default=0, help="仅测试前 N 只")
    ap.add_argument("--out-dir", default="", help="结果输出目录")
    ap.add_argument("--pool", type=int, default=POOL_MIN_SCORE,
                    help="观察池门槛（满足 N/%d 条），设 %d 关闭观察池"
                         % (len(COND_NAMES), len(COND_NAMES)))
    ap.add_argument("--loose", action="store_true",
                    help="宽松模式：换手率 3%~15%%、缺口允许部分回补（命中数约提高一个量级）")
    args = ap.parse_args()

    if args.loose:
        changed = []
        for key, val in LOOSE_PRESET.items():
            old = globals()[key]
            globals()[key] = val
            changed.append("%s %s → %s" % (key, old, val))
        print("已启用宽松模式：" + "；".join(changed))

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
    print("主板 + 非ST + 流通市值 %g~%g 亿 初筛后：%d 只"
          % (NMC_MIN_YI, NMC_MAX_YI, len(cands)))
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
            if done[0] % 100 == 0 or done[0] == total:
                print("  进度 %d/%d  用时 %.0fs" % (done[0], total, time.time() - t0))
        time.sleep(SLEEP_SEC)
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for r in pool.map(work, cands):
            if r:
                rows.append(r)

    hits = [r for r in rows if r["score"] == len(COND_NAMES)]
    hits.sort(key=lambda r: (-r["chg_pct"], r["code"]))
    near = [r for r in rows if args.pool <= r["score"] < len(COND_NAMES)]
    near.sort(key=lambda r: (-r["score"], -r["chg_pct"], r["code"]))

    hit_path = os.path.join(out_dir, "命中_%s.csv" % tag)
    write_csv(hits, hit_path, CSV_FIELDS)

    pool_path = ""
    if near:
        pool_path = os.path.join(out_dir, "观察池_%s.csv" % tag)
        write_csv(near, pool_path, POOL_FIELDS)

    print_funnel(rows, total)
    skipped = [(k, stats.get(k, 0)) for k in SKIP_KEYS if stats.get(k)]
    if skipped:
        print("  跳过：" + "，".join("%s %d 只" % kv for kv in skipped))

    print("\n" + "=" * 58)
    if hits:
        print("命中 %d 只：" % len(hits))
        for r in hits:
            print("  %s %-6s %s 收 %.2f 涨 %.2f%% 换手 %.2f%% 量比 %.2f "
                  "缺口 %s(%.1f%%,%s日) %s"
                  % (r["code"], r["name"], r["base_date"], r["close"], r["chg_pct"],
                     r["turnover_pct"], r["vol_ratio"], r["gap_date"], r["gap_pct"],
                     r["gap_age"], r["macd_type"]))
    else:
        print("命中 0 只 —— 五条条件同时成立的概率较低，属正常结果")

    if near:
        print("\n观察池 %d 只（满足 %d/%d 条）"
              % (len(near), args.pool, len(COND_NAMES)))
        for r in near[:20]:
            print("  %s %-6s 涨 %5.2f%% 换手 %5.2f%% 量比 %4.2f  缺：%s"
                  % (r["code"], r["name"], r["chg_pct"], r["turnover_pct"],
                     r["vol_ratio"], r["missing"]))
        if len(near) > 20:
            print("  …… 其余 %d 只见 CSV" % (len(near) - 20))

    print("\n用时 %.0fs" % (time.time() - t0))
    print("命中文件：%s" % hit_path)
    if pool_path:
        print("观察池文件：%s" % pool_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已手动终止")
        sys.exit(1)
