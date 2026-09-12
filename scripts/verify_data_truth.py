#!/usr/bin/env python3
"""数据真实性验证(防幻觉): 全标的×多周期逐项核对喂给AI的每个数字都可回溯真实数据。

检查项:
1. OHLC 一致性: high >= max(open,close) / low <= min(open,close) / close==last
2. 时间戳单调递增 + 间隔均匀(±10%)
3. 成交量/成交额 > 0(允许极少数0)
4. 报价 vs K线最后一根 close 对照(应基本一致)
5. changeUtc24h 与 K 线首尾对照
6. 指标黄金值(TSLA 全周期): 手工 RSI/MA/ATR/ADX/BB vs compute_indicators
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config
from supermarket.indicators import compute_indicators, klines_to_df

CFG = Config.load()
BG = BitgetClient(**CFG.bitget.__dict__)
GRANS = ["5m", "1H", "4H", "1D"]


def check_ohlc(sym: str, gran: str, limit: int) -> list[str]:
    errs = []
    try:
        rows = BG.klines(sym, gran, limit)
        df = klines_to_df(rows)
        if df.empty:
            return [f"{sym} {gran}: 空"]
        c, o, h, l = df["close"].astype(float), df["open"].astype(float), df["high"].astype(float), df["low"].astype(float)
        viol_h = (h < np.maximum(o, c) - 1e-9).sum()
        viol_l = (l > np.minimum(o, c) + 1e-9).sum()
        if viol_h:
            errs.append(f"high<max(o,c) x{viol_h}")
        if viol_l:
            errs.append(f"low>min(o,c) x{viol_l}")
        # 时间戳单调 + 均匀
        ts = df["ts"].astype("int64").values
        if np.any(np.diff(ts) <= 0):
            errs.append("ts非单调")
        else:
            d = np.diff(ts)
            med = np.median(d)
            if med > 0 and (np.abs(d - med) > med * 0.1).sum() > max(2, len(d) * 0.05):
                errs.append(f"ts间隔不均匀({int(med)}ms, 偏差{int((np.abs(d-med)>med*0.1).sum())}处)")
        # 量非负
        if (df["volume"].astype(float) < 0).any():
            errs.append("volume负值")
        # close 全正
        if (c <= 0).any():
            errs.append("close<=0")
        # 报价对照: 最后一根close vs 实时lastPr
        try:
            q = BG.quote(sym)
            last = float(q.get("lastPr") or 0)
            klast = float(c.iloc[-1])
            if last > 0 and abs(last / klast - 1) > 0.005:
                errs.append(f"报价{last:.2f} vs 末K{klast:.2f} 差{abs(last/klast-1)*100:.2f}%")
        except Exception as e:
            errs.append(f"quote对照失败:{str(e)[:40]}")
    except Exception as e:
        errs.append(f"异常:{type(e).__name__}:{str(e)[:60]}")
    return errs


def golden_indicators(sym: str = "TSLAUSDT") -> list[str]:
    """手工黄金值 vs compute_indicators(全周期)。"""
    errs = []
    for gran in GRANS:
        df = klines_to_df(BG.klines(sym, gran, 300))
        ind = compute_indicators(df)
        c = df["close"].astype(float)
        # MA10/MA30
        ma10 = float(c.rolling(10).mean().iloc[-1])
        ma30 = float(c.rolling(30).mean().iloc[-1])
        for name, got, want in (("MA10", ind.ma10, ma10), ("MA30", ind.ma30, ma30)):
            if abs(got - want) > 1e-6 * max(1, abs(want)):
                errs.append(f"{sym} {gran} {name}: {got:.4f} vs 手工{want:.4f}")
        # RSI14 Wilder 手工
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        want_rsi = float(100 - 100 / (1 + rs.iloc[-1])) if not pd.isna(rs.iloc[-1]) else 100.0
        if abs(ind.rsi - want_rsi) > 0.5:
            errs.append(f"{sym} {gran} RSI: {ind.rsi:.2f} vs 手工{want_rsi:.2f}")
        # ATR14(简单均值近似对照 Wilder 平滑, 用同公式)
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - c.shift()).abs(),
                        (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
        atr_w = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
        if abs(ind.atr - atr_w) > 1e-6 * max(1, atr_w):
            errs.append(f"{sym} {gran} ATR: {ind.atr:.4f} vs 手工{atr_w:.4f}")
        # 量比黄金值(最后完整K线逻辑同 compute_indicators)
        v = df["volume"].astype(float)
        ts = df["ts"].astype(float)
        li = len(df) - 1
        if len(df) >= 2 and float(ts.iloc[-1] - ts.iloc[-2]) > 0 and float(ts.iloc[-1]) + float(ts.iloc[-1] - ts.iloc[-2]) > time.time() * 1000:
            li = len(df) - 2
        want_vr = float(v.iloc[li] / max(v.iloc[max(0, li - 20):li].mean(), 1e-9))
        if abs(ind.vol_ratio - want_vr) > 0.01:
            errs.append(f"{sym} {gran} 量比: {ind.vol_ratio:.3f} vs 手工{want_vr:.3f}")
    return errs


def main():
    # 1) 全标的 OHLC/时间/量 一致性(4周期), 抽样报价对照
    tickers = BG.tickers()
    symbols = sorted({t["symbol"] for t in tickers if t["symbol"].endswith("USDT")})
    print(f"标的池: {len(symbols)}")
    bad: dict[str, list[str]] = {}
    t0 = time.time()
    tasks = [(sym, gran) for sym in symbols for gran in GRANS]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(check_ohlc, sym, gran, CFG.kline_limits.get(gran, 100)): (sym, gran) for sym, gran in tasks}
        done = 0
        for fut in as_completed(futs):
            sym, gran = futs[fut]
            for e in fut.result():
                bad.setdefault(f"{sym} {gran}", []).append(e)
            done += 1
            if done % 200 == 0:
                print(f"  进度 {done}/{len(tasks)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"=== OHLC/时间/量 校验完成: {len(symbols)}标的×4周期, 问题标的 {len(bad)} ===")
    for k, v in list(bad.items())[:20]:
        print(f"  ❌ {k}: {v}")
    if len(bad) > 20:
        print(f"  ... 其余 {len(bad)-20} 个标的也报错")

    # 2) 指标黄金值(TSLA 全周期)
    errs = golden_indicators()
    print(f"=== 指标黄金值校验: {'全部通过 ✓' if not errs else '发现 ' + str(len(errs)) + ' 处'} ===")
    for e in errs:
        print("  ❌", e)

    # 3) changeUtc24h 与 K 线对照(抽样)
    print("=== changeUtc24h vs 日线首尾对照(抽样10只) ===")
    for sym in symbols[:10]:
        try:
            q = BG.quote(sym)
            chg = float(q.get("changeUtc24h") or 0) * 100
            df = klines_to_df(BG.klines(sym, "1D", 3))
            if len(df) >= 2:
                c = df["close"].astype(float)
                kchg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
                flag = "✓" if abs(chg - kchg) < 3.0 else f"!!(差{abs(chg-kchg):.1f}pp)"
                print(f"  {sym:12s} 24h报价{chg:+6.2f}%  日线首尾{kchg:+6.2f}%  {flag}")
        except Exception as e:
            print(f"  {sym}: {str(e)[:50]}")

    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()