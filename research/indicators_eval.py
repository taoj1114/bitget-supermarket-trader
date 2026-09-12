#!/usr/bin/env python3
"""指标有效性评估(用户方法论): 多标的×多时段, feature×fwd胜率桶。

对每个标的每个历史K线, 计算当前指标值, 打桶(如 RSI<30/30-40/.../70+),
统计每桶未来 fwd 根K线(如5日/20根4H)上涨概率 —— 找出有区分度(feature能预测fwd)的指标,
淘汰无区分度(所有桶胜率≈50%)的指标, 并确定最优阈值。

产出: research/ind_eval_report.md + CSV
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config
from supermarket.indicators import _wilder_smooth, klines_to_df

CFG = Config.load()
BG = BitgetClient(**CFG.bitget.__dict__)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """逐行计算指标(向量化), 返回新增列。"""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    out = pd.DataFrame(index=df.index)
    out["rsi14"] = _rsi(c, 14)
    out["ma5"] = c.rolling(5).mean()
    out["ma10"] = c.rolling(10).mean()
    out["ma20"] = c.rolling(20).mean()
    out["ma30"] = c.rolling(30).mean()
    out["ma50"] = c.rolling(50).mean()
    out["dev_ma30"] = (c / out["ma30"] - 1) * 100
    out["dev_ma20"] = (c / out["ma20"] - 1) * 100
    out["adx14"] = _adx(h, l, c, 14)
    atr = _wilder_smooth(pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1), 14)
    out["atr_pct"] = atr / c * 100
    out["vol_r"] = v / v.rolling(20).mean()
    # BB 位置 (20,2σ)
    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    out["bb_pos"] = (c - mid) / (2 * std).replace(0, np.nan)
    out["macd"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    out["macd_sig"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_o"] = (out["macd"] - out["macd_sig"]) / c * 100
    out["chg5"] = (c / c.shift(5) - 1) * 100       # 近5周期涨幅(动量)
    out["chg20"] = (c / c.shift(20) - 1) * 100      # 近20周期涨幅(趋势)
    out["dist_hi20"] = (c / h.rolling(20).max() - 1) * 100   # 距20周期高点
    out["dist_lo20"] = (c / l.rolling(20).min() - 1) * 100   # 距20周期低点
    return out


def _rsi(c: pd.Series, n: int) -> pd.Series:
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _adx(h, l, c, n=14) -> pd.Series:
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_s = _wilder_smooth(tr, n)
    pdi = _wilder_smooth(pd.Series(plus_dm, index=h.index), n) / atr_s.replace(0, np.nan) * 100
    mdi = _wilder_smooth(pd.Series(minus_dm, index=h.index), n) / atr_s.replace(0, np.nan) * 100
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    return _wilder_smooth(dx.fillna(0), n)


BUCKETS = {
    "rsi14": [(0, 30, "RSI<30(超卖)"), (30, 40, "30-40"), (40, 50, "40-50"), (50, 60, "50-60"), (60, 70, "60-70"), (70, 200, "RSI>70(超买)")],
    "dev_ma30": [(-99, -10, "低于MA30>10%"), (-10, -5, "-10~-5%"), (-5, 0, "-5~0%"), (0, 5, "0~5%"), (5, 10, "5~10%"), (10, 99, "高于MA30>10%")],
    "dist_hi20": [(-99, -10, "距高>-10%"), (-10, -5, "-10~-5"), (-5, -3, "-5~-3"), (-3, -1, "-3~-1"), (-1, 0, "-1~0"), (0, 99, "创新高")],
    "adx14": [(0, 15, "ADX<15(衰竭)"), (15, 20, "15-20"), (20, 25, "20-25"), (25, 35, "25-35(趋势)"), (35, 99, ">35(强趋势)")],
    "atr_pct": [(0, 1, "ATR<1%"), (1, 2, "1-2%"), (2, 4, "2-4%"), (4, 7, "4-7%"), (7, 99, ">7%(剧烈)")],
    "macd_o": [(-99, -0.5, "MACD<-0.5%"), (-0.5, -0.1, "-0.5~-0.1"), (-0.1, 0.1, "~0"), (0.1, 0.5, "0.1~0.5"), (0.5, 99, "MACD>0.5%")],
    "chg20": [(-99, -20, "20周期<-20%"), (-20, -10, "-20~-10"), (-10, 0, "-10~0"), (0, 10, "0~10"), (10, 20, "10~20"), (20, 99, ">20%")],
    "vol_r": [(0, 0.5, "量0.5x"), (0.5, 0.8, "0.5-0.8x"), (0.8, 1.2, "正常1x"), (1.2, 2, "1.2-2x"), (2, 99, ">2x放量")],
    "bb_pos": [(-99, -1, "BB<-1(下轨外)"), (-1, -0.5, "-1~-0.5"), (-0.5, 0, "-0.5~0"), (0, 0.5, "0~0.5"), (0.5, 1, "0.5~1"), (1, 99, "BB>1(上轨外)")],
    "dist_lo20": [(-99, 0, "创新低"), (0, 1, "0~1%"), (1, 3, "1~3%"), (3, 5, "3~5%"), (5, 99, ">5%")],
}


def run(symbols: list[str], gran: str, nbars: int, fwd: int, label: str) -> pd.DataFrame:
    """采集样本: 每标的末尾 nbars 根, 特征+未来fwd根首尾收益。"""
    records = []
    t0 = time.time()
    for i, sym in enumerate(symbols):
        try:
            df = klines_to_df(BG.klines(sym, gran, nbars + fwd))
            if len(df) < nbars + fwd:
                continue
            feats = compute_features(df)
            c = df["close"].astype(float)
            fwd_ret = (c.shift(-fwd) / c - 1) * 100  # 未来fwd根收益率
            fet = feats.iloc[-nbars:]
            fr = fwd_ret.iloc[-nbars:]
            base = feats.loc[fet.index, ["rsi14", "dev_ma30", "dist_hi20", "adx14", "atr_pct",
                                         "macd_o", "chg20", "vol_r", "bb_pos", "dist_lo20"]]
            for idx in fet.index:
                r = base.loc[idx]
                if any(pd.isna(r)):
                    continue
                rec = {k: float(v) for k, v in r.items()}
                rec["ret_fwd"] = float(fr.loc[idx]) if not pd.isna(fr.loc[idx]) else np.nan
                rec["sym"] = sym
                records.append(rec)
        except Exception as e:
            print(f"  {sym} 失败: {str(e)[:60]}")
        if (i + 1) % 40 == 0:
            print(f"  {label} 进度 {i+1}/{len(symbols)} ({time.time()-t0:.0f}s) 样本{len(records)}")
    return pd.DataFrame(records)


def evaluate(df: pd.DataFrame, gran_label: str, fwd: int):
    rows = []
    for feat, buckets in BUCKETS.items():
        if feat not in df.columns:
            continue
        sub = df[df[feat].notna() & df["ret_fwd"].notna()]
        for lo, hi, bname in buckets:
            b = sub[(sub[feat] >= lo) & (sub[feat] < hi)]
            if len(b) < 30:
                continue
            wr = float((b["ret_fwd"] > 0).mean()) * 100
            med = float(b["ret_fwd"].median())
            rows.append({"gran": gran_label, "fwd": fwd, "feature": feat, "bucket": bname,
                         "n": len(b), "win_rate": wr, "med_ret": med})
    return pd.DataFrame(rows)


def main():
    tickers = BG.tickers()
    symbols = sorted({t["symbol"] for t in tickers if t["symbol"].endswith("USDT")})
    # 与美股池一致: 只保留池内(有 blacklist 清洗), 简化: 取前120只流动性好的美股
    vol = {t["symbol"]: float(t.get("usdtVolume", 0) or 0) for t in tickers}
    symbols = sorted(symbols, key=lambda s: -vol.get(s, 0))[:120]
    print(f"研究标的: {len(symbols)} (流动性前120)")

    all_rows = []
    # 日线: 90根可用, 取末尾60根做样本, fwd=5(5个交易日)
    d1 = run(symbols, "1D", 65, 5, "1D")
    all_rows.append(evaluate(d1, "日线", 5))
    # 4H: 540根, 取末尾400根, fwd=10(≈2.5日→接近超市持仓期)
    d4 = run(symbols, "4H", 400, 10, "4H")
    all_rows.append(evaluate(d4, "4H", 10))

    out = pd.concat(all_rows, ignore_index=True)
    out.to_csv("research/ind_eval.csv", index=False)
    # 报告: 各特征的美化胜率矩阵
    lines = ["# 指标有效性评估报告", "",
             f"标的 {len(symbols)} × 日线(65根采样,fwd5日) + 4H(400根,fwd10根)  (生成于 {time.strftime('%Y-%m-%d %H:%M')})", ""]
    for gran in ("日线", "4H"):
        lines.append(f"## {gran} (fwd={5 if gran=='日线' else 10})")
        lines.append("| 特征 | 桶(胜率% / n) |")
        lines.append("|---|---|")
        for feat, buckets in BUCKETS.items():
            g = out[(out.gran == gran) & (out.feature == feat)]
            if g.empty:
                continue
            cells = []
            for _, r in g.iterrows():
                cells.append(f"{r.bucket}:{r.win_rate:.0f}%(n{r.n})")
            lines.append(f"| {feat} | {' · '.join(cells)} |")
        lines.append("")
    Path("research/ind_eval_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("报告写至 research/ind_eval_report.md, 数据 research/ind_eval.csv")


if __name__ == "__main__":
    main()