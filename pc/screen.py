# -*- coding: utf-8 -*-
"""
沪深主板股票筛选脚本（数据源：新浪财经）

筛选条件：
  1. 沪深主板（沪 600/601/603/605，深 000/001/002）
  2. 股价 3 元 ~ 30 元
  3. 流通市值 30 亿 ~ 200 亿
  4. 最近 20 个交易日内，五日均线(MA5) 第二次 上穿 十日均线(MA10)
     即：最新一天刚发生金叉，且最近 20 根日 K 内累计恰为第 2 次金叉
  5. 最新一天 MACD 同时发生金叉（DIF 上穿 DEA）
  6. 近 20 个交易日内出现过涨停板（主板 10% 口径）
  7. 剔除 ST、*ST 等风险警示股

仅使用 Python 标准库，无第三方依赖。
运行：python screen.py
"""

import csv
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ================= 可配置参数 =================
PRICE_MIN = 3.0          # 最低股价（元）
PRICE_MAX = 30.0         # 最高股价（元）
NMC_MIN = 300000.0       # 最小流通市值（万元，30亿 = 300000万）
NMC_MAX = 2000000.0      # 最大流通市值（万元，200亿 = 2000000万）
K_DAYS = 60              # 最低需要的日 K 根数（用于计算均线余量）
K_FETCH_DAYS = 500       # 实际拉取的日 K 根数（支持历史日期回溯）
SIGNAL_WINDOW = 20       # 信号观察窗口（最近 N 个交易日）
GOLDEN_CROSS_TARGET = 2  # 窗口内需达到的金叉次数
MACD_FAST = 6            # MACD 快线 EMA 周期
MACD_SLOW = 13           # MACD 慢线 EMA 周期
MACD_SIGNAL = 5          # MACD 信号线 DEA 的 EMA 周期
SLEEP_SEC = 0.3          # 每次 K 线请求间隔（秒），避免被封
LIST_PAGE_SIZE = 100     # 列表接口每页数量
MAX_RETRY = 3            # 单次请求最大重试次数

# 沪深主板代码前缀
SH_MAIN = ("600", "601", "603", "605")
SZ_MAIN = ("000", "001", "002")

LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
COUNT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/Market_Center.getHQNodeStockCount")
KLINE_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/"
             "CN_MarketData.getKLineData")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
}

# K 线接口备用域名不需要 Referer，仅带 User-Agent 即可
KLINE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}


def http_get_json(url, headers=None, retries=MAX_RETRY):
    """GET 请求并解析 JSON，失败自动重试。"""
    headers = headers or HEADERS
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            text = raw.decode("utf-8", errors="replace").strip()
            # 新浪接口有时带 BOM 或 JS 变量包裹，此处一律按 JSON 处理
            return json.loads(text)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def get_total_count():
    """获取沪深 A 股总数量，接口可能返回字符串或整数。"""
    params = urllib.parse.urlencode({"node": "hs_a"})
    raw = http_get_json(f"{COUNT_URL}?{params}")
    # 正常返回为字符串形式的数字，例如 "5549"
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    # 异常时返回 {"__ERROR": 1, ...} 结构
    if isinstance(raw, dict):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_stock_list():
    """分页拉取全市场沪深 A 股列表。"""
    total = get_total_count()
    if total is None:
        total = 6000  # 无法获取总数时给一个足够大的兜底
    print(f"沪深 A 股总数约: {total}")

    stocks = []
    page = 1
    while len(stocks) < total:
        params = urllib.parse.urlencode({
            "page": page,
            "num": LIST_PAGE_SIZE,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
        })
        url = f"{LIST_URL}?{params}"
        data = http_get_json(url)
        if not data:
            break
        stocks.extend(data)
        if len(data) < LIST_PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)
    print(f"实际拉取到 {len(stocks)} 只")
    return stocks


def is_main_board(code):
    """判断是否为沪深主板股票。"""
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(SH_MAIN) or code.startswith(SZ_MAIN)


def to_sina_symbol(code):
    """600xxx -> sh600xxx, 000xxx -> sz000xxx。"""
    if code.startswith(("6", "5")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    return "sh" + code


def fetch_kline(symbol):
    """
    拉取某只股票的日 K 线，返回按时间升序的列表，
    每个元素为 {"day": "YYYY-MM-DD", "close": float}。
    """
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "scale": 240,   # 日线
        "ma": "no",
        "datalen": K_FETCH_DAYS,
    })
    url = f"{KLINE_URL}?{params}"
    data = http_get_json(url, headers=KLINE_HEADERS)
    if not isinstance(data, list) or not data:
        return []
    bars = []
    for bar in data:
        try:
            day = str(bar.get("day", "")).strip()
            close = float(bar.get("close", 0))
            if day:
                bars.append({"day": day, "close": close})
        except (TypeError, ValueError):
            continue
    return bars


def calc_ma(values, period):
    """计算移动平均线（与收盘价列表等长，前 period-1 个为 None）。"""
    ma = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        ma[i] = sum(window) / period
    return ma


def calc_ema(values, period):
    """计算指数移动平均线（以首个价格为种子）。"""
    ema = [None] * len(values)
    if not values:
        return ema
    k = 2.0 / (period + 1)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def macd_golden_on_latest(closes):
    """
    判断最新一天是否发生 MACD 金叉（DIF 上穿 DEA）。
    参数：EMA6、EMA13、DEA=EMA5(DIF)。
    """
    n = len(closes)
    if n < 2:
        return False
    ema_fast = calc_ema(closes, MACD_FAST)
    ema_slow = calc_ema(closes, MACD_SLOW)
    dif = [a - b for a, b in zip(ema_fast, ema_slow)]
    dea = calc_ema(dif, MACD_SIGNAL)
    i = n - 1
    return dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]


def count_golden_cross_in_window(ma5, ma10, window):
    """
    在最近 window 根 K 线内统计金叉次数。
    金叉定义：当日 MA5 > MA10 且前一日 MA5 <= MA10。
    返回 (金叉次数, 最新一天是否为金叉)。
    """
    n = len(ma5)
    if n < window + 1:
        return 0, False

    start = n - window  # 观察窗口的起点下标（含）
    golden_indices = []
    for i in range(max(1, start), n):
        if ma5[i] is None or ma10[i] is None:
            continue
        if ma5[i - 1] is None or ma10[i - 1] is None:
            continue
        if ma5[i] > ma10[i] and ma5[i - 1] <= ma10[i - 1]:
            golden_indices.append(i)

    latest_golden = bool(golden_indices) and golden_indices[-1] == n - 1
    return len(golden_indices), latest_golden


def check_signal(closes):
    """
    返回是否满足信号条件：
    最新一天 MA5 上穿 MA10，且最近 SIGNAL_WINDOW 天内恰为第 2 次金叉。
    """
    if len(closes) < K_DAYS:
        return False
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    count, latest_golden = count_golden_cross_in_window(ma5, ma10, SIGNAL_WINDOW)
    # 定义 A：最新一天 MA5 上穿 MA10，且最近 20 日内恰为第 2 次金叉
    if not (latest_golden and count == GOLDEN_CROSS_TARGET):
        return False
    # 新增：最新一天 MACD 同时发生金叉（DIF 上穿 DEA）
    return macd_golden_on_latest(closes)


def has_limit_up_in_window(closes, window):
    """
    判断最近 window 个交易日内是否出现过涨停板。
    主板涨停幅度 10%，按涨幅 >= 9.8% 判定（规避涨停价四舍五入误差）。
    """
    n = len(closes)
    if n < 2:
        return False
    start = max(1, n - window)
    for i in range(start, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        if (closes[i] - prev) / prev >= 0.098:
            return True
    return False


def get_output_path(as_of=None):
    """结果文件以时间命名，优先保存到桌面，确保在别的电脑上也能用。"""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    base = desktop if os.path.isdir(desktop) else os.getcwd()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if as_of:
        label = f"股票筛选结果_{as_of.replace('-', '')}_{ts}"
    else:
        label = f"股票筛选结果_{ts}"
    return os.path.join(base, label + ".csv")


def prompt_date():
    """提示用户输入筛选基准日期，格式 YYYY-MM-DD。"""
    while True:
        raw = input("请输入筛选基准日期（格式 YYYY-MM-DD，例如 2026-08-28）：").strip()
        try:
            d = datetime.datetime.strptime(raw, "%Y-%m-%d")
            return d.strftime("%Y-%m-%d")
        except ValueError:
            print("日期格式错误，请按 YYYY-MM-DD 格式重新输入。")


def slice_kline_by_date(bars, as_of):
    """截取 day <= as_of 的 K 线（按时间升序）。"""
    return [b for b in bars if b["day"] <= as_of]


def main():
    as_of = prompt_date()
    print(f"基准日期：{as_of}（取该日或之前最近交易日）")
    output = get_output_path(as_of)
    stocks = fetch_stock_list()

    candidates = []
    for s in stocks:
        code = str(s.get("code", ""))
        if not is_main_board(code):
            continue
        name = s.get("name", "")
        # 剔除 ST、*ST 等风险警示股
        if "ST" in name.upper():
            continue
        try:
            nmc = float(s.get("nmc", 0) or 0)  # 单位：万元
        except (TypeError, ValueError):
            continue

        # 初筛：仅按流通市值过滤（股价留到拉取 K 线后按基准日收盘价判定）
        if nmc <= 0 or not (NMC_MIN <= nmc <= NMC_MAX):
            continue

        candidates.append({
            "code": code,
            "symbol": to_sina_symbol(code),
            "name": name,
            "nmc_yi": round(nmc / 10000, 2),  # 转为亿元
        })

    print(f"流通市值初筛后剩余: {len(candidates)} 只")

    results = []
    for idx, c in enumerate(candidates, 1):
        try:
            bars = fetch_kline(c["symbol"])
        except Exception as e:  # noqa: BLE001
            print(f"[{idx}/{len(candidates)}] {c['code']} {c['name']} K线获取失败: {e}")
            continue

        bars = slice_kline_by_date(bars, as_of)
        if len(bars) < K_DAYS:
            continue

        closes = [b["close"] for b in bars]
        base_close = closes[-1]   # 基准日收盘价（截止日或之前最近交易日）
        base_date = bars[-1]["day"]

        # 股价最终以基准日收盘价判断
        if not (PRICE_MIN <= base_close <= PRICE_MAX):
            continue

        try:
            ok_signal = check_signal(closes)
            ok_limit = has_limit_up_in_window(closes, SIGNAL_WINDOW)
        except Exception as e:  # noqa: BLE001
            print(f"[{idx}/{len(candidates)}] {c['code']} 信号计算失败: {e}")
            continue

        if ok_signal and ok_limit:
            hit = dict(c)
            hit["base_close"] = round(base_close, 2)
            hit["base_date"] = base_date
            results.append(hit)
            print(f"[{idx}/{len(candidates)}] 命中: {c['code']} {c['name']} "
                  f"基准日 {base_date} 收盘 {hit['base_close']} 流通市值 {c['nmc_yi']}亿")

        if idx % 20 == 0:
            print(f"进度: {idx}/{len(candidates)}")
        time.sleep(SLEEP_SEC)

    # 输出 CSV
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["代码", "名称", "基准日期", "收盘价", "流通市值(亿)"])
        for r in results:
            writer.writerow([r["code"], r["name"], r["base_date"], r["base_close"], r["nmc_yi"]])

    print("\n" + "=" * 40)
    print(f"筛选完成，共命中 {len(results)} 只，结果见 {output}")
    if results:
        for r in results:
            print(f"{r['code']} {r['name']}  {r['base_date']} 收盘 {r['base_close']}  流通市值 {r['nmc_yi']} 亿")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已手动终止")
        sys.exit(1)