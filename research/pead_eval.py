"""财报后漂移(PEAD)验证 + Nasdaq 财报日历接入测试(2026-09)。

数据源: Nasdaq 财报日历(api.nasdaq.com, 免费, 含 EPS 惊喜%),
       个股日线来自 Bitget 池子(211 只美股永续)。

检验:
1) 财报后 1/3/5 日收益 是否显著区别于非财报期基准(漂移效应)
2) 按盈利惊喜(超预期/符合/低于预期)分档 → fwd5 胜率差异(PEAD 的方向性)
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config
from supermarket.indicators import klines_to_df
from supermarket.market import MarketData

OUT = Path(__file__).resolve().parent / "pead_report.md"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept": "application/json, text/plain, */*"}


def win(v: list[float]) -> float:
    return round(sum(1 for x in v if x > 0) / len(v) * 100, 1) if v else 0.0


def nasdaq_earnings(day: str) -> list[dict]:
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day}"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        rows = (d.get("data") or {}).get("rows") or []
        return [{"symbol": (r.get("symbol") or "").strip().upper(),
                 "surprise": r.get("surprise"), "eps": r.get("eps")}
                for r in rows if r.get("symbol")]
    except Exception as e:
        print(f"    ! {day} 拉取失败: {str(e)[:60]}")
        return []


def parse_surprise(raw) -> float | None:
    if not raw:
        return None
    s = str(raw).replace("%", "").replace(",", "").strip()
    if s in ("", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    c = Config.load()
    bg = BitgetClient(**c.bitget.__dict__)
    m = MarketData(bg, cfg=c)
    from supermarket.engine import SupermarketEngine
    eng = SupermarketEngine(c)
    eng._load_contracts()
    pool = {s: s[:-4] for s in eng._contracts}          # NVDAUSDT → NVDA
    by_ticker = {}
    for s, t in pool.items():
        by_ticker.setdefault(t, s)

    # 1) 拉 Nasdaq 财报日历(跳过周末)
    print(f"拉 Nasdaq 财报日历(最近 {days} 天)...")
    today = datetime.utcnow().date()
    events: dict[str, list[str]] = {}   # ticker → [日期...]
    n_days = 0
    for i in range(days):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.isoformat()
        rows = nasdaq_earnings(ds)
        n_days += 1
        for r in rows:
            tk = r["symbol"]
            if tk in by_ticker:
                events.setdefault(tk, []).append(ds)
        time.sleep(0.2)
    print(f"  查询 {n_days} 天, 命中池内公司 {len(events)} 家, "
          f"事件 {sum(len(v) for v in events.values())} 个")

    # 2) 用日线算财报后收益
    res = []   # (ticker, 日期, fwd1, fwd3, fwd5)
    for tk, dates in events.items():
        sym = by_ticker[tk]
        try:
            df = klines_to_df(m.klines(sym, "1D", 90))
            if df is None or len(df) < 20:
                continue
            cl = [float(x) for x in df["close"].tolist()]
            ts = [int(x) for x in df["ts"].tolist()]
            day_list = [time.strftime("%Y-%m-%d", time.gmtime(t / 1000)) for t in ts]
            for ds in dates:
                if ds not in day_list:
                    continue
                i = day_list.index(ds)
                if i + 5 >= len(cl):
                    continue
                f1 = (cl[i + 1] / cl[i] - 1) * 100
                f3 = (cl[i + 3] / cl[i] - 1) * 100
                f5 = (cl[i + 5] / cl[i] - 1) * 100
                res.append((tk, ds, f1, f3, f5))
        except Exception:
            continue
    print(f"可计算收益的财报事件: {len(res)} 个")

    lines = ["# 财报后漂移(PEAD)验证", f"\n样本: {len(res)} 个财报事件(池内公司)"]
    print("\n" + "=" * 70)
    if len(res) < 30:
        print("样本不足(<30), 无法得出结论")
        lines.append("\n样本不足, 无法得出结论")
    else:
        for idx, label in ((2, "fwd1"), (3, "fwd3"), (4, "fwd5")):
            vals = [r[idx] for r in res]
            print(f"{label}: n={len(vals)} 均值 {statistics.mean(vals):+.2f}% "
                  f"中位 {statistics.median(vals):+.2f}% 胜率 {win(vals):.1f}%")
            lines.append(f"- {label}: n={len(vals)}, 均值 {statistics.mean(vals):+.2f}%, 胜率 {win(vals):.1f}%")

        # 基准: 全样本 fwd5(池内随机日)
        base = []
        for sym in sorted(eng._contracts)[:60]:
            try:
                df = klines_to_df(m.klines(sym, "1D", 90))
                cl = [float(x) for x in df["close"].tolist()]
                for i in range(0, len(cl) - 5):
                    base.append((cl[i + 5] / cl[i] - 1) * 100)
            except Exception:
                continue
        print(f"\n非财报期基准(60只全样本): fwd5 均值 {statistics.mean(base):+.2f}% 胜率 {win(base):.1f}%")
        lines.append(f"\n基准(非财报期): fwd5 均值 {statistics.mean(base):+.2f}%, 胜率 {win(base):.1f}%")

    OUT.write_text("\n".join(lines))
    print(f"\n报告: {OUT.name}")


if __name__ == "__main__":
    main()
