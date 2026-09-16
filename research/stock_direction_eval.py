"""个股层面方向性信息预测力研究(2026-09)。

验证候选: 相对强度(RS vs SPY) / 放量突破 / 多因子组合 / 位置。
方法: Bitget 美股池日线 90 根, 每(股票,日)样本 → fwd5 收益分桶。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config
from supermarket.indicators import klines_to_df
from supermarket.market import MarketData

OUT = Path(__file__).resolve().parent / "stock_direction_report.md"


def win(vals: list[float]) -> float:
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else 0.0


def main() -> None:
    c = Config.load()
    bg = BitgetClient(**c.bitget.__dict__)
    m = MarketData(bg, cfg=c)
    from supermarket.engine import SupermarketEngine
    eng = SupermarketEngine(c)
    eng._load_contracts()
    pool = sorted(eng._contracts)
    print(f"池 {len(pool)} 只, 拉日线...")

    spy = klines_to_df(m.klines("SPYUSDT", "1D", 90))
    spy_close = {int(r.ts): float(r.close) for r in spy.itertuples()}
    spy_days = sorted(spy_close)

    def spy_ret20(ts: int) -> float | None:
        """SPY 从该日往前 20 日的收益(按时间戳就近匹配)。"""
        cands = [d for d in spy_days if d <= ts]
        if len(cands) < 21:
            return None
        cur, prev = cands[-1], cands[-21]
        return (spy_close[cur] / spy_close[prev] - 1) * 100

    buckets: dict[str, list[float]] = {}
    n_ok = 0
    for sym in pool:
        try:
            df = klines_to_df(m.klines(sym, "1D", 90))
            if df is None or len(df) < 40:
                continue
            n_ok += 1
            cl = [float(x) for x in df["close"].tolist()]
            vol = [float(x) for x in df["volume"].tolist()]
            ts = [int(x) for x in df["ts"].tolist()]
            hi = [float(x) for x in df["high"].tolist()]
            for i in range(20, len(cl) - 5):
                fwd = (cl[i + 5] / cl[i] - 1) * 100
                ret20 = (cl[i] / cl[i - 20] - 1) * 100
                sr = spy_ret20(ts[i])
                if sr is None:
                    continue
                # 1) 相对强度(20日超额收益)
                rs = ret20 - sr
                b1 = ("RS<-10%" if rs < -10 else "RS-10~-3%" if rs < -3 else
                      "RS-3~3%" if rs < 3 else "RS3~10%" if rs < 10 else "RS>10%")
                buckets.setdefault(f"相对强度RS|{b1}", []).append(fwd)
                # 2) 放量突破(收盘创20日新高 且 量比>1.5)
                hh = max(hi[i - 20:i])
                vr = vol[i] / (sum(vol[i - 20:i]) / 20) if sum(vol[i - 20:i]) else 0
                if cl[i] >= hh and vr >= 1.5:
                    buckets.setdefault("放量突破|是", []).append(fwd)
                elif cl[i] >= hh:
                    buckets.setdefault("放量突破|缩量新高", []).append(fwd)
                else:
                    buckets.setdefault("放量突破|否", []).append(fwd)
                # 3) 位置(距20日最高)
                pos = (cl[i] / max(hi[i - 20:i + 1]) - 1) * 100
                b3 = ("位-1~0%" if pos > -1 else "位-1~-5%" if pos > -5 else
                      "位-5~-10%" if pos > -10 else "位<-10%")
                buckets.setdefault(f"距20日高|{b3}", []).append(fwd)
                # 4) 多因子组合(趋势+RSI+乖离, 复用指标经验)
                ma30 = sum(cl[i - 29:i + 1]) / 30
                gains = [max(cl[k] - cl[k - 1], 0) for k in range(i - 13, i + 1)]
                losses = [max(cl[k - 1] - cl[k], 0) for k in range(i - 13, i + 1)]
                ag, al = sum(gains) / 14, sum(losses) / 14
                rsi = 100 - 100 / (1 + ag / al) if al else 100
                bias = (cl[i] / ma30 - 1) * 100
                combo = (cl[i] > ma30 and 40 <= rsi <= 55 and -5 <= bias <= 5)
                buckets.setdefault(f"三因子组合|{'命中' if combo else '未命中'}", []).append(fwd)
        except Exception:
            continue
    print(f"有效标的 {n_ok}")

    lines = ["# 个股方向性信息预测力(Bitget 池, fwd5)", ""]
    print("\n" + "=" * 76)
    feats: dict[str, list[tuple[str, list[float]]]] = {}
    for k, v in buckets.items():
        feat, b = k.split("|", 1)
        feats.setdefault(feat, []).append((b, v))
    for feat, items in feats.items():
        print(f"\n【{feat}】")
        lines.append(f"\n## {feat}")
        for b, v in sorted(items, key=lambda x: -win(x[1])):
            if len(v) < 30:
                continue
            print(f"  {b:14s} n={len(v):5d}  均值 {statistics.mean(v):+.2f}%  "
                  f"中位 {statistics.median(v):+.2f}%  胜率 {win(v):.1f}%")
            lines.append(f"- {b}: n={len(v)}, fwd5 {statistics.mean(v):+.2f}%, 胜率 {win(v):.1f}%")
        valid = [(b, v) for b, v in items if len(v) >= 30]
        if len(valid) >= 2:
            valid.sort(key=lambda x: -win(x[1]))
            spread = win(valid[0][1]) - win(valid[-1][1])
            vd = "✅ 有预测力" if spread >= 8 else ("⚠️ 弱" if spread >= 4 else "❌ 无区分度")
            print(f"  → 极差 {spread:.1f}pct {vd}")
            lines.append(f"- **极差 {spread:.1f}pct → {vd}**")
    OUT.write_text("\n".join(lines))
    print(f"\n报告: {OUT.name}")


if __name__ == "__main__":
    main()
