"""BTC/ETH 对美股方向的预测力验证(2026-09)。

系统输入里接了 SPY/QQQ/BTC/ETH,但 BTC/ETH 的预测力从未验证。
若证伪 → 从 AI 输入中删除(用户方法论: 无预测力的信息不进系统)。

检验:
1) BTC/ETH 日收益 与 池内个股日收益 的相关性(时间序列)
2) BTC/ETH 前日涨跌分桶 → 池内个股 fwd5 胜率(与 SPY 前日涨跌作基准对比)
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config
from supermarket.indicators import klines_to_df
from supermarket.market import MarketData

OUT = Path(__file__).resolve().parent / "btc_relevance_report.md"


def win(v: list[float]) -> float:
    return round(sum(1 for x in v if x > 0) / len(v) * 100, 1) if v else 0.0


def corr(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 5:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    return round(num / (dx * dy), 3) if dx and dy else 0.0


def main() -> None:
    c = Config.load()
    bg = BitgetClient(**c.bitget.__dict__)
    m = MarketData(bg, cfg=c)
    from supermarket.engine import SupermarketEngine
    eng = SupermarketEngine(c)
    eng._load_contracts()

    macro = {}
    for s in ("SPYUSDT", "BTCUSDT", "ETHUSDT"):
        df = klines_to_df(m.klines(s, "1D", 90))
        macro[s] = [float(x) for x in df["close"].tolist()]
    print(f"宏观序列: SPY/BTC/ETH 各 {len(macro['SPYUSDT'])} 根")

    # 日收益
    rets = {}
    for s, cl in macro.items():
        rets[s] = [(cl[i] / cl[i - 1] - 1) * 100 for i in range(1, len(cl))]

    rows = {"BTCUSDT": [], "ETHUSDT": [], "SPYUSDT": []}   # (bucket, fwd5)
    pool_ret_by_day: dict[int, list[float]] = {}
    n_ok = 0
    for sym in sorted(eng._contracts):
        try:
            df = klines_to_df(m.klines(sym, "1D", 90))
            if df is None or len(df) < 40:
                continue
            n_ok += 1
            cl = [float(x) for x in df["close"].tolist()]
            for i in range(1, len(cl) - 5):
                pool_ret_by_day.setdefault(i, []).append((cl[i] / cl[i - 1] - 1) * 100)
                fwd = (cl[i + 5] / cl[i] - 1) * 100
                for mac in ("BTCUSDT", "ETHUSDT", "SPYUSDT"):
                    r = rets[mac]
                    if i - 1 < 0 or i - 1 >= len(r):
                        continue
                    prev = r[i - 1]
                    b = ("跌>3%" if prev < -3 else "跌1~3%" if prev < -1 else
                         "平±1%" if prev < 1 else "涨1~3%" if prev < 3 else "涨>3%")
                    rows[mac].append((b, fwd))
        except Exception:
            continue
    print(f"有效标的 {n_ok}")

    lines = ["# BTC/ETH/SPY 对美股方向的预测力(池内个股 fwd5)", ""]
    print("\n" + "=" * 74)
    summary = {}
    for mac in ("BTCUSDT", "ETHUSDT", "SPYUSDT"):
        g: dict[str, list[float]] = {}
        for b, r in rows[mac]:
            g.setdefault(b, []).append(r)
        print(f"\n【{mac} 前日涨跌 → 池内个股 fwd5】")
        lines.append(f"\n## {mac}")
        stats = []
        for b in ("跌>3%", "跌1~3%", "平±1%", "涨1~3%", "涨>3%"):
            v = g.get(b, [])
            if len(v) < 30:
                continue
            stats.append((b, win(v), len(v), statistics.mean(v)))
            print(f"  {b:8s} n={len(v):5d}  fwd5均值 {statistics.mean(v):+.2f}%  胜率 {win(v):.1f}%")
            lines.append(f"- {b}: n={len(v)}, fwd5 {statistics.mean(v):+.2f}%, 胜率 {win(v):.1f}%")
        if len(stats) >= 2:
            spread = max(s[1] for s in stats) - min(s[1] for s in stats)
            vd = "✅ 有预测力" if spread >= 8 else ("⚠️ 弱" if spread >= 4 else "❌ 无区分度")
            summary[mac] = (spread, vd)
            print(f"  → 极差 {spread:.1f}pct {vd}")
            lines.append(f"- **极差 {spread:.1f}pct → {vd}**")

    # 相关性: BTC 日收益 vs 池内当日中位收益
    pool_daily = [statistics.median(v) for _, v in sorted(pool_ret_by_day.items())]
    print("\n【相关性(BTC/ETH/SPY 日收益 vs 池内中位日收益)】")
    for mac in ("BTCUSDT", "ETHUSDT", "SPYUSDT"):
        cc = corr(rets[mac], pool_daily)
        print(f"  {mac:8s} r = {cc:+.3f}")
        lines.append(f"- 相关性 {mac}: r = {cc:+.3f}")

    OUT.write_text("\n".join(lines))
    print(f"\n报告: {OUT.name}")
    print("\n=== 结论 ===")
    for mac, (sp, vd) in summary.items():
        print(f"  {mac:8s} 胜率极差 {sp:5.1f}pct  {vd}")


if __name__ == "__main__":
    main()
