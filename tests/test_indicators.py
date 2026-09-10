"""指标层测试: 确定性合成 K 线断言语义正确性 + 反转K线识别。"""

import pandas as pd
import numpy as np

from supermarket.indicators import (
    compute_indicators,
    klines_to_df,
    reversal_kline,
    trend_shape,
    _volume_status,
)


def make_df(closes, opens=None, highs=None, lows=None, vols=None):
    n = len(closes)
    opens = opens or [c for c in closes]
    highs = highs or [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    vols = vols or [100.0] * n
    return pd.DataFrame({
        "ts": list(range(n)),
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })


def test_uptrend_indicators():
    closes = [100 + i * 0.5 for i in range(60)]
    df = make_df(closes)
    ind = compute_indicators(df, primary=True)
    assert ind.rsi > 55
    assert ind.ma10 > ind.ma30
    assert ind.regime == "trend_up"
    assert ind.adx > 20
    assert ind.change_pct > 0
    assert ind.bias_ma30 > 0


def test_downtrend_regime():
    closes = [100 - i * 0.7 for i in range(60)]
    ind = compute_indicators(make_df(closes), primary=True)
    assert ind.rsi < 45
    assert ind.regime == "trend_down"


def test_flat_regime():
    closes = [100 + (i % 5 - 2) * 0.1 for i in range(60)]
    ind = compute_indicators(make_df(closes), primary=True)
    assert ind.regime == "flat"


def test_volume_status_down():
    closes = [100 + i for i in range(30)]
    vols = [100.0] * 28 + [300.0, 285.0]
    df = make_df(closes, vols=vols)
    df.iloc[29, df.columns.get_loc("close")] = 100 + 28  # 最后一根阴线
    vs = _volume_status(df)
    assert "放量" in vs and "下跌" in vs


def test_trend_shape_reversal():
    """先涨后跌 → 反转回落。"""
    closes = [100 + i * 0.3 for i in range(12)] + [103.5 - i * 0.4 for i in range(12)]
    df = make_df(closes)
    shape = trend_shape(df, lookback=24)
    assert "反转" in shape
    assert "关键位" in shape


def test_reversal_kline_top():
    """前段上涨 + 高位小实体长上影 + 放量 → 顶部反转信号。"""
    closes = [100 + i * 0.5 for i in range(16)]  # 前段明显上涨
    closes[-1] = 108.0
    df = make_df(closes)
    # 构造候选K线: 高开低走, 小实体, 长上影, 放量
    df.iloc[-1, df.columns.get_loc("open")] = 108.2
    df.iloc[-1, df.columns.get_loc("high")] = 111.0
    df.iloc[-1, df.columns.get_loc("low")] = 106.5
    df.iloc[-1, df.columns.get_loc("close")] = 108.0
    df.iloc[-1, df.columns.get_loc("volume")] = 500.0
    sig = reversal_kline(df)
    assert "反转" in sig and "顶部" in sig


def test_reversal_kline_no_false_midrange():
    """窗口中部的小实体长影 → 不触发(位置门控)。"""
    closes = [100 + i * 0.5 for i in range(16)]
    df = make_df(closes)
    for r in range(len(df) - 12, len(df)):
        df.iloc[r, df.columns.get_loc("high")] = float(df.iloc[r]["close"]) + 1.0
        df.iloc[r, df.columns.get_loc("low")] = float(df.iloc[r]["close"]) - 1.0
    assert reversal_kline(df) == ""


def test_klines_to_df_strings():
    rows = [
        {"ts": "1000", "open": "10", "high": "11", "low": "9", "close": "10.5",
         "baseVolume": "100", "usdtVolume": "1000"},
        {"ts": "2000", "open": "10.5", "high": "12", "low": "10", "close": "11.5",
         "baseVolume": "120", "usdtVolume": "1300"},
    ]
    df = klines_to_df(rows)
    assert df["close"].dtype == np.float64
    assert len(df) == 2
    assert float(df["close"].iloc[-1]) == 11.5