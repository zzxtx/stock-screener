# -*- coding: utf-8 -*-
"""
离线自检：不需要联网，用构造的 K 线验证四条条件的判定逻辑，以及筹码集中度的方向性。

运行：python selftest.py

构造的正样本形态：
  长期横盘（筹码堆在 10 元附近）→ 涨停（不跳空）→ 次日跳空 → 两天窄幅整理 → 今日
  其中涨停在 3 个交易日前，缺口在涨停次日形成且未被回补。

每个用例只改动一个变量，验证对应条件的开关是否生效。
"""

import datetime
import sys
import threading

import screen as S

BASE_DATE = datetime.date(2024, 1, 1)
AS_OF = "2030-12-31"


def d(i):
    return (BASE_DATE + datetime.timedelta(days=i)).isoformat()


def build(flat=140, base=10.0, lu_mult=1.10, cons_lo=0.995, cons_hi=1.005,
          vol_base=4e7, vol_cons=4e7, hold_days=2, gap_extra=1.005,
          prev_limit=False, fill_gap=False, wide_last=False,
          gap_at_limit_up=False):
    """按固定剧本生成 K 线；参数用来故意破坏某一条条件。"""
    bars = []

    def add(o, h, l, c, v):
        bars.append(dict(day="", open=o, high=h, low=l, close=c, volume=v))

    for _ in range(flat):
        add(base, base * 1.02, base * 0.98, base, vol_base)

    if prev_limit:
        # 让涨停日的前一天也是涨停，构成连板
        bars[-1]["close"] = base * 1.10
        bars[-1]["high"] = base * 1.10

    prev_high = bars[-1]["high"]
    t_close = base * (lu_mult * 1.10 if prev_limit else lu_mult)

    # ---- 涨停日 T ----
    if gap_at_limit_up:
        # 缺口放在涨停当日（新规则下应被淘汰）
        add(prev_high * 1.02, t_close, prev_high * gap_extra, t_close, vol_cons * 1.5)
        gap_top = None
    else:
        # 涨停日不跳空：最低价低于前一日最高价
        add(prev_high * 0.995, t_close, prev_high * 0.99, t_close, vol_cons * 1.5)
        gap_top = t_close * gap_extra        # 次日最低价，即缺口上沿

    # ---- 涨停次日 T+1：跳空 ----
    if gap_top is None:
        g_close = t_close * 1.01
        add(t_close, g_close * 1.01, t_close * 0.999, g_close, vol_cons)
    else:
        g_close = t_close * 1.03
        add(t_close * 1.01, g_close * 1.02, gap_top, g_close, vol_cons * 1.2)

    # ---- 整理 ----
    for k in range(hold_days):
        c = g_close * (1 + 0.001 * k)
        lo = c * cons_lo
        hi = c * cons_hi
        if fill_gap and k == hold_days - 1:
            lo = t_close * 0.999            # 跌回缺口区，构成回补
        if wide_last and k == hold_days - 1:
            lo, hi = (gap_top if gap_top else t_close), c * 1.12   # 最后一天放宽带
        add(c, hi, lo, c, vol_cons)

    for i, b in enumerate(bars):
        b["day"] = d(i)
    return bars


def run_case(bars, nmc_yi=80.0):
    cand = {"code": "600000", "name": "测试股", "symbol": "sh600000",
            "nmc_yi": nmc_yi, "nmc_yuan": nmc_yi * 1e4}
    S.fetch_kline = lambda symbol: [dict(b) for b in bars]
    return S.evaluate(cand, AS_OF, {}, threading.Lock())


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s %s" % ("OK  " if cond else "FAIL", name, detail))
        if not cond:
            fails.append(name)

    total = len(S.COND_NAMES)

    print("== 1. 正样本：四条应全部满足 ==")
    r = run_case(build())
    check("score == %d" % total, r is not None and r["score"] == total,
          "" if r is None else "score=%d 未满足=%s" % (r["score"], r["missing"] or "无"))
    if r:
        print("      收 %.2f | 涨停 %s（%s 日前）| 缺口 %s +%.2f%%（%s 日前）"
              "| 集中度 %s → %s"
              % (r["close"], r["limit_date"], r["limit_age"], r["gap_date"],
                 r["gap_pct"], r["gap_age"], r["scr_prev"], r["scr_today"]))

    print("== 2. 股价边界（3 < 股价 < 25） ==")
    for name, kw, want in [
        ("股价 2.86 淘汰", dict(base=2.6), False),
        ("股价 11.02 通过", dict(base=10.0), True),
        ("股价 25.85 淘汰", dict(base=23.5), False),
    ]:
        rr = run_case(build(**kw))
        check(name, rr is not None and rr["flags"]["股价"] == want,
              "实测 %.2f" % rr["close"] if rr else "")

    print("== 3. 涨停条件 ==")
    rr = run_case(build(prev_limit=True))
    check("连板（前一日也涨停）淘汰", rr is not None and not rr["flags"]["涨停"],
          "" if rr is None else "涨停距今 %s 日" % rr["limit_age"])
    rr = run_case(build(hold_days=0))
    check("涨停仅 1 日前 淘汰", rr is not None and not rr["flags"]["涨停"],
          "" if rr is None else "涨停距今 %s 日" % rr["limit_age"])
    rr = run_case(build(hold_days=1))
    check("涨停 2 日前 通过", rr is not None and rr["flags"]["涨停"],
          "" if rr is None else "涨停距今 %s 日" % rr["limit_age"])

    print("== 4. 缺口（只认涨停次日） ==")
    rr = run_case(build(gap_at_limit_up=True))
    check("缺口在涨停当日 淘汰", rr is not None and not rr["flags"]["缺口"],
          "（新规则只认 T+1 跳空）")
    rr = run_case(build())
    check("缺口在涨停次日 通过", rr is not None and rr["flags"]["缺口"],
          "" if rr is None else "缺口 %s +%.2f%%（%s 日前）"
          % (rr["gap_date"], rr["gap_pct"], rr["gap_age"]))
    rr = run_case(build(fill_gap=True))
    check("缺口已回补 淘汰", rr is not None and not rr["flags"]["缺口"], "")
    rr = run_case(build(gap_extra=1.001))
    check("缺口 0.1% 淘汰（低于 0.2% 阈值）", rr is not None and not rr["flags"]["缺口"],
          "" if rr is None else "缺口幅度 %s" % (rr["gap_pct"] or "-"))
    rr = run_case(build(gap_extra=1.005))
    check("缺口 0.5% 通过", rr is not None and rr["flags"]["缺口"],
          "" if rr is None else "缺口幅度 %s" % rr["gap_pct"])

    print("== 5. 筹码集中度 ==")
    rr = run_case(build(wide_last=True))
    check("最后一天放宽带 → 集中度上升 淘汰", rr is not None and not rr["flags"]["筹码"],
          "" if rr is None else "集中度 %s → %s" % (rr["scr_prev"], rr["scr_today"]))
    rr = run_case(build())
    check("窄幅整理 → 集中度下降 通过", rr is not None and rr["flags"]["筹码"],
          "" if rr is None else "集中度 %s → %s" % (rr["scr_prev"], rr["scr_today"]))

    print("== 6. 筹码分布计算 ==")
    chips = [0.0, 0.0, 1.0, 2.0, 1.0, 0.0]
    scr, p5, p95 = S._scr(chips, 10.0, 0.5)
    check("P5 < P95", p5 < p95, "P5=%.2f P95=%.2f" % (p5, p95))
    check("集中度在 (0,100) 内", 0 < scr < 100, "SCR=%.2f" % scr)
    wide = [0.2] * 12
    scr2, _, _ = S._scr(wide, 10.0, 0.5)
    check("分布越宽 集中度越大", scr2 > scr, "宽 %.2f > 窄 %.2f" % (scr2, scr))

    print()
    if fails:
        print("自检失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("自检全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
