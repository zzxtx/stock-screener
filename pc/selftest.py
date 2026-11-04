# -*- coding: utf-8 -*-
"""
离线自检：不需要联网，用构造的 K 线验证五条条件的判定逻辑。

运行：python selftest.py

构造的正样本形态（与实盘无关，只为覆盖每个分支）：
  长期下跌 → 横盘 → 一根涨停 → 回调 → 向上跳空缺口（未回补）→ 整理 → 当日 +4%
  当日成交量略高于 5 日均量，换手率约 7%

每个用例只改动一个变量，验证对应条件的开关是否生效。
"""

import datetime
import sys
import threading

import screen as S

BASE_DATE = datetime.date(2024, 1, 1)
AS_OF = "2030-12-31"

VOL_BASE = 5e7
VOL_LAST = 7e7
FLOAT_SHARES = 1e9        # 固定流通股本，使换手率 = 成交量 / 1e9


def d(i):
    return (BASE_DATE + datetime.timedelta(days=i)).isoformat()


def build(flat=20, dec_to=8.85, dec_days=3, gap_pct=0.3, hold=3, chg=0.04,
          vol_base=VOL_BASE, vol_last=VOL_LAST, fill_gap=False):
    """按固定剧本生成 K 线；参数用来故意破坏某一条条件。"""
    bars = []

    def add(o, h, l, c, v):
        bars.append(dict(day="", open=o, high=h, low=l, close=c, volume=v))

    n_down = 150
    for i in range(n_down):
        c = 11.0 - (11.0 - 9.0) * i / (n_down - 1)
        add(c, c * 1.004, c * 0.996, c, vol_base)
    for _ in range(flat):
        add(9.0, 9.02, 8.98, 9.0, vol_base)

    # 一根放量长阳（+9.8%），随后回调 —— 只是为了让 MACD 处在低位
    top = 9.0 * 1.098
    add(9.0, top, 9.0, top, vol_base * 1.6)

    # 长阳后回调
    for i in range(dec_days):
        c = top - (top - dec_to) * i / max(1, dec_days - 1)
        add(c, c * 1.004, c * 0.996, c, vol_base)

    # 向上跳空缺口
    base_high = bars[-1]["high"]
    g = base_high * (1 + gap_pct / 100.0)
    floor = g * 1.0005
    add(g, g * 1.004, g, g, vol_base)
    for _ in range(hold):
        add(g, g * 1.004, floor, g, vol_base)

    last = g * (1 + chg)
    last_low = base_high * 0.999 if fill_gap else floor
    add(g, last * 1.002, last_low, last, vol_last)

    for i, b in enumerate(bars):
        b["day"] = d(i)
    return bars


def run_case(bars):
    """用构造数据跑一次完整判定。"""
    nc = bars[-1]["close"]
    cand = {"code": "600000", "name": "测试股", "symbol": "sh600000",
            "nmc_yi": round(FLOAT_SHARES * nc / 1e4, 2), "nmc_yuan": FLOAT_SHARES * nc}
    S.fetch_kline = lambda symbol: [dict(b) for b in bars]
    return S.evaluate(cand, AS_OF, {}, threading.Lock())


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s %s" % ("OK  " if cond else "FAIL", name, detail))
        if not cond:
            fails.append(name)

    print("== 1. 正样本：五条应全部满足 ==")
    r = run_case(build())
    total = len(S.COND_NAMES)
    check("score == %d" % total, r is not None and r["score"] == total,
          "" if r is None else "score=%d 未满足=%s" % (r["score"], r["missing"] or "无"))
    if r:
        print("      收 %.2f 涨 %.2f%% 换手 %.2f%% 量比 %.2f 缺口 %s(+%.2f%%,%s日前) "
              "%s DIF %.3f"
              % (r["close"], r["chg_pct"], r["turnover_pct"], r["vol_ratio"],
                 r["gap_date"], r["gap_pct"], r["gap_age"], r["macd_type"], r["dif"]))

    print("== 2. 涨幅边界 ==")
    for name, kw, want in [
        ("涨 1.0% 淘汰", dict(chg=0.010), False),
        ("涨 2.0% 通过(下边界)", dict(chg=0.020), True),
        ("涨 5.0% 通过(上边界)", dict(chg=0.050), True),
        ("涨 5.5% 淘汰", dict(chg=0.055), False),
    ]:
        rr = run_case(build(**kw))
        got = rr is not None and rr["flags"]["涨幅"]
        check(name, got == want, "实测 %.3f%%" % (rr["chg_pct"] if rr else -1))

    print("== 3. 成交量（换手率保持 7%%，只动量能） ==")
    for name, kw, want in [
        ("量比 2.14 倍 淘汰", dict(vol_base=2e7, vol_last=6e7), False),
        ("量比 0.90 倍 淘汰", dict(vol_base=8e7, vol_last=7e7), False),
    ]:
        rr = run_case(build(**kw))
        got = rr is not None and rr["flags"]["量能"]
        check(name, got == want,
              "实测 量比 %.2f 换手 %.2f%%" % (rr["vol_ratio"], rr["turnover_pct"]) if rr else "")

    print("== 4. 缺口 ==")
    for name, kw, want in [
        ("缺口 0.10% 淘汰(噪声)", dict(gap_pct=0.10), False),
        ("缺口 0.30% 通过", dict(gap_pct=0.30), True),
        ("缺口已回补 淘汰", dict(fill_gap=True), False),
        ("缺口 2 日前 通过", dict(hold=1), True),
        ("缺口 1 日前 淘汰", dict(hold=0), False),
    ]:
        rr = run_case(build(**kw))
        got = rr is not None and rr["flags"]["缺口"]
        check(name, got == want, "实测缺口天数 %s" % (rr["gap_age"] if rr else "?"))

    print("== 5. MACD 高位金叉排除 ==")
    rising = [10.0 * (1.012 ** i) for i in range(120)]
    ok, _ = S.check_macd(rising)
    dif, _, _ = S.calc_macd(rising)
    check("单边上涨的零轴上方不算", not ok,
          "DIF %.3f > 阈值 %.3f" % (dif[-1], rising[-1] * S.DIF_ZERO_BAND_PCT))

    print()
    if fails:
        print("自检失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("自检全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
