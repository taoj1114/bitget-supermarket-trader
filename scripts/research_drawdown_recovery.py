#!/usr/bin/env python3
"""回撤-修复统计研究: 美股永续日线, 从高点回撤 X% 后, 未来 N 日收复概率 vs 继续深跌概率。

用户命题: "正常美股从高点回撤~50%都会修复, 我们一般不买在最高点, 所以单票跌10%+可接受,
大概率不会再跌" — 用 Bitget 美股永续真实日线验证(多标的 × 回撤深度桶 × 恢复窗口)。
并给出每个桶的 [最差继续跌幅, 收复所需时间中位数] 供止损/拿住决策参考。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from supermarket.bitget_client import BitgetClient  # noqa: E402
from supermarket.config import Config  # noqa: E402
from supermarket.indicators import klines_to_df  # noqa: E402


def main() -> None:
    cfg = Config.load("config.yaml")
    bg = BitgetClient(cfg.bitget.base_url, cfg.bitget.api_key,
                      cfg.bitget.secret, cfg.bitget.passphrase)

    # 按 24h 成交额取流动性 top N 美股
    ticks = {t["symbol"]: t for t in bg.tickers()}
    stock = []
    for c in bg.stock_contracts():
        t = ticks.get(c["symbol"])
        if t:
            stock.append((c["symbol"], float(t.get("usdtVolume", 0) or 0)))
    stock.sort(key=lambda x: -x[1])
    top = stock[:60]
    print(f"研究样本: 流动性 top {len(top)} 美股永续\n")

    events: list[dict] = []  # 每个回撤事件的后续统计
    n_klines = 0
    for i, (sym, vol) in enumerate(top):
        rows = bg.klines(sym, "1D", 200)
        df = klines_to_df(rows)
        if len(df) < 40:
            continue
        n_klines += len(df)
        close = df["close"].values
        ts = df["ts"].values
        n = len(close)
        # 滚动最高点(用截至当日的前高: rolling max 含当日 = 用 shift)
        roll_high = pd.Series(close).rolling(60, min_periods=10).max().shift(1)
        for j in range(30, n - 1):
            hi = roll_high.iloc[j]
            if not hi or hi <= 0:
                continue
            drawdown = close[j] / hi - 1  # 负值
            if drawdown < -0.08:  # 只记录回撤 ≥8% 的事件(每只票可能几十个)
                # 未来 5/20/60 个交易日的修复与继续深跌
                fut5 = close[j + 1:j + 6]
                fut20 = close[j + 1:j + 21]
                fut60 = close[j + 1:j + 61]
                events.append({
                    "symbol": sym, "dd": drawdown, "ts": ts[j],
                    "rec5": (fut5.max() >= hi) if len(fut5) else None,
                    "rec20": (fut20.max() >= hi) if len(fut20) else None,
                    "rec60": (fut60.max() >= hi) if len(fut60) else None,
                    "max_further": (fut60.min() / close[j] - 1) if len(fut60) else None,
                })
        time.sleep(0.08)

    if not events:
        print("无事件样本"); return
    df = pd.DataFrame(events)
    print(f"K线总数 {n_klines}, 回撤≥8% 事件 {len(df)} 个(去重按票不按事件重叠? 保留全事件)\n")

    def bucket(dd: float) -> str:
        d = -dd * 100
        if d < 10: return "8-10%"
        if d < 15: return "10-15%"
        if d < 20: return "15-20%"
        if d < 30: return "20-30%"
        return "30%+"

    df["bucket"] = df["dd"].map(bucket)
    print(f"{'回撤桶':<8} {'事件':>5} {'5日收复':>8} {'20日收复':>8} {'60日收复':>8} "
          f"{'继续深跌≥10%':>10} {'继续深跌≥20%':>10} {'再创新低中位':>10}")
    for b in ["8-10%", "10-15%", "15-20%", "20-30%", "30%+"]:
        sub = df[df["bucket"] == b]
        if len(sub) == 0:
            continue
        rec5 = sub["rec5"].notna().sum()
        rec20 = sub["rec20"].notna().sum()
        rec60 = sub["rec60"].notna().sum()
        further10 = (sub["max_further"] < -0.10).sum()
        further20 = (sub["max_further"] < -0.20).sum()
        sec = pd.to_numeric(sub["max_further"], errors="coerce")
        print(f"{b:<8} {len(sub):>5} "
              f"{sub['rec5'].sum() / rec5 * 100:>7.0f}% {sub['rec20'].sum() / rec20 * 100:>7.0f}% "
              f"{sub['rec60'].sum() / rec60 * 100:>7.0f}% "
              f"{further10 / len(sub) * 100:>9.0f}% {further20 / len(sub) * 100:>9.0f}% "
              f"{sec.median() * 100:>9.1f}%")

    # 结论摘要: 10-15% 回撤桶的明细(用户命题核心)
    core = df[df["bucket"] == "10-15%"]
    print("\n===== 用户命题检验: 10-15% 回撤后 =====")
    if len(core):
        rec20 = core["rec20"].dropna()
        rec60 = core["rec60"].dropna()
        further = pd.to_numeric(core["max_further"], errors="coerce").dropna()
        print(f"20日内收复前高概率: {len(rec20[rec20])}/{len(rec20)} = {rec20.mean() * 100:.0f}%")
        print(f"60日内收复前高概率: {len(rec60[rec60])}/{len(rec60)} = {rec60.mean() * 100:.0f}%")
        print(f"期间继续深跌中位: {further.median() * 100:.1f}% (最差分位再跌 {further.quantile(0.9) * 100:.1f}%)")
        print(f"‘继续跌≥10%’概率: {(further < -0.10).mean() * 100:.0f}%")
        print(f"样本最差: 有一只继续跌 {further.min() * 100:.0f}%")
        syms = core["symbol"].nunique()
        print(f"覆盖标的数: {syms}(多标的×多时段 ✓)")
    print("\n注: 恢复窗口含资金费率成本未计入(8h结算, 持有N天≈N×0.01%成本)")

    # 落盘供报告
    out = Path(__file__).resolve().parent.parent / "research" / "drawdown_recovery.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, index=False)
    print(f"明细已存: {out}")


if __name__ == "__main__":
    main()