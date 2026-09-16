"""方向性信息预测力研究(2026-09, 用户要求: 还有什么能准确提供方向)。

方法: 每个候选特征分桶 → 统计 fwd5(未来5个交易日)收益的均值/胜率/样本数。
判据(与 ind_eval 一致): 桶间差异明显且样本充足(≥30)才算有预测力; 无区分度 → 否决。

数据:
- 市场级(Yahoo, 2 年日线): SPY/QQQ/^VIX/^VIX3M/^TNX/DX-Y.NYB
- 个股级(Bitget 日线 90 根): 美股池 211 只 → 池内宽度
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

OUT_MD = Path(__file__).resolve().parent / "direction_inputs_report.md"
OUT_CSV = Path(__file__).resolve().parent / "direction_inputs.csv"

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2y"


def yahoo_daily(sym: str) -> dict[str, float]:
    """取 Yahoo 日线收盘: {YYYY-MM-DD: close}。"""
    url = YF.format(sym=urllib.parse.quote(sym))
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        out[day] = float(c)
    return out


def pct_change_series(s: dict[str, float]) -> dict[str, float]:
    days = sorted(s)
    out = {}
    for i in range(1, len(days)):
        prev, cur = s[days[i - 1]], s[days[i]]
        if prev:
            out[days[i]] = (cur / prev - 1) * 100
    return out


def fwd_return(s: dict[str, float], day: str, n: int = 5) -> float | None:
    days = sorted(s)
    if day not in days:
        return None
    i = days.index(day)
    if i + n >= len(days):
        return None
    a, b = s[days[i]], s[days[i + n]]
    return (b / a - 1) * 100 if a else None


def bucket_stats(rows: list[tuple[str, float]]) -> list[dict]:
    """rows = [(bucket_label, fwd_return)] → 统计。"""
    groups: dict[str, list[float]] = {}
    for label, r in rows:
        groups.setdefault(label, []).append(r)
    out = []
    for label, vals in groups.items():
        if not vals:
            continue
        out.append({
            "bucket": label, "n": len(vals),
            "mean": round(statistics.mean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            "std": round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0,
        })
    return out


def main() -> None:
    print("拉取 Yahoo 宏观数据(2年日线)...")
    syms = {"SPY": "SPY", "QQQ": "QQQ", "VIX": "^VIX", "VIX3M": "^VIX3M",
            "TNX": "^TNX", "DXY": "DX-Y.NYB"}
    data: dict[str, dict[str, float]] = {}
    for k, s in syms.items():
        try:
            data[k] = yahoo_daily(s)
            print(f"  ✓ {k:6s} {len(data[k])} 天")
        except Exception as e:
            print(f"  ✗ {k:6s} 失败: {str(e)[:60]}")
    if "SPY" not in data:
        print("SPY 数据缺失, 终止")
        return

    spy = data["SPY"]
    vix = data.get("VIX", {})
    vix3m = data.get("VIX3M", {})
    tnx = data.get("TNX", {})
    dxy = data.get("DXY", {})
    vix_chg = pct_change_series(vix)
    tnx_chg = pct_change_series(tnx)
    dxy_chg = pct_change_series(dxy)
    spy_chg = pct_change_series(spy)

    # SPY 自身 regime(用 20 日累计涨幅近似, 避免再实现 ADX)
    spy_days = sorted(spy)
    feat_rows: list[tuple[str, str, float]] = []  # (feature, bucket, fwd5)

    for day in spy_days:
        fwd = fwd_return(spy, day, 5)
        if fwd is None:
            continue
        # 1) SPY 20 日动量
        i = spy_days.index(day)
        if i >= 20:
            mom20 = (spy[day] / spy[spy_days[i - 20]] - 1) * 100
            b = "跌<-8%" if mom20 < -8 else ("跌-8~-3%" if mom20 < -3 else
                                             ("震荡-3~3%" if mom20 < 3 else
                                              ("涨3~8%" if mom20 < 8 else "涨>8%")))
            feat_rows.append(("SPY_20日动量", b, fwd))
        # 2) VIX 水平
        if day in vix:
            v = vix[day]
            b = "<15(平静)" if v < 15 else ("15-18" if v < 18 else ("18-22" if v < 22 else
                                                                  ("22-28" if v < 28 else ">28(恐慌)")))
            feat_rows.append(("VIX水平", b, fwd))
            # 3) VIX 日变化
            if day in vix_chg:
                c = vix_chg[day]
                b = "VIX急升>10%" if c > 10 else ("升3~10%" if c > 3 else
                                              ("平稳-3~3%" if c > -3 else
                                               ("降3~10%" if c > -10 else "VIX急降>10%")))
                feat_rows.append(("VIX日变化", b, fwd))
            # 4) VIX/VIX3M 期限结构(>1 = 近月恐慌 > 远月 = 压力大)
            if day in vix3m and vix3m[day]:
                ratio = v / vix3m[day]
                b = "<0.90" if ratio < 0.90 else ("0.90-0.95" if ratio < 0.95 else
                                                  ("0.95-1.0" if ratio < 1.0 else "≥1.0(倒挂)"))
                feat_rows.append(("VIX期限结构", b, fwd))
        # 5) 美债收益率变化
        if day in tnx_chg:
            c = tnx_chg[day]
            b = "急升>3%" if c > 3 else ("升1~3%" if c > 1 else
                                      ("平稳±1%" if c > -1 else ("降1~3%" if c > -3 else "急降>3%")))
            feat_rows.append(("10年美债日变化", b, fwd))
        # 6) 美元指数变化
        if day in dxy_chg:
            c = dxy_chg[day]
            b = "DXY升>1%" if c > 1 else ("DXY升0.3~1%" if c > 0.3 else
                                        ("DXY稳±0.3%" if c > -0.3 else
                                         ("DXY降0.3~1%" if c > -1 else "DXY降>1%")))
            feat_rows.append(("美元指数日变化", b, fwd))
        # 7) 隔夜自身跳空(前一日 SPY 涨跌)
        if day in spy_chg:
            c = spy_chg[day]
            b = "前日跌>2%" if c < -2 else ("前日跌1~2%" if c < -1 else
                                          ("前日平±1%" if c < 1 else ("前日涨1~2%" if c < 2 else "前日涨>2%")))
            feat_rows.append(("SPY前日涨跌", b, fwd))

    # 汇总
    by_feature: dict[str, list[tuple[str, float]]] = {}
    for feat, b, fwd in feat_rows:
        by_feature.setdefault(feat, []).append((b, fwd))

    lines = ["# 方向性信息预测力研究(SPY fwd5, 2年样本)", "",
             "方法: 每个特征分桶 → 未来5交易日 SPY 收益(均值/胜率)。",
             "判据: 桶间胜率差异 ≥8pct 且样本 ≥30 → 有预测力。", ""]
    csv_rows = ["feature,bucket,n,mean_fwd5,median_fwd5,win_rate,std"]
    print("\n" + "=" * 78)
    verdicts = []
    for feat, rows in by_feature.items():
        stats = bucket_stats(rows)
        stats.sort(key=lambda x: -x["win_rate"])
        print(f"\n【{feat}】")
        for s in stats:
            print(f"  {s['bucket']:16s} n={s['n']:4d}  fwd5均值 {s['mean']:+.2f}%  "
                  f"中位 {s['median']:+.2f}%  胜率 {s['win_rate']:.1f}%")
            csv_rows.append(f"{feat},{s['bucket']},{s['n']},{s['mean']},{s['median']},"
                            f"{s['win_rate']},{s['std']}")
            lines.append(f"- {feat} | {s['bucket']} | n={s['n']} | fwd5 {s['mean']:+.2f}% | 胜率 {s['win_rate']:.1f}%")
        if len(stats) >= 2 and min(s["n"] for s in stats) >= 30:
            spread = stats[0]["win_rate"] - stats[-1]["win_rate"]
            verdict = ("✅ 有预测力" if spread >= 8 else "⚠️ 弱" if spread >= 4 else "❌ 无区分度")
            print(f"  → 胜率极差 {spread:.1f}pct {verdict}")
            verdicts.append((feat, spread, verdict))
            lines.append(f"- **{feat} 胜率极差 {spread:.1f}pct → {verdict}**")
        else:
            print("  → 样本不足")

    OUT_CSV.write_text("\n".join(csv_rows))
    OUT_MD.write_text("\n".join(lines))
    print(f"\n报告: {OUT_MD.name} / {OUT_CSV.name}")
    print("\n=== 结论排序(胜率极差) ===")
    for feat, spread, verdict in sorted(verdicts, key=lambda x: -x[1]):
        print(f"  {spread:5.1f}pct  {feat:16s} {verdict}")


if __name__ == "__main__":
    import urllib.parse
    main()
