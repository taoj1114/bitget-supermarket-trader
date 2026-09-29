"""反转检测器的去噪测试(2026-09-29 实测 MSFT 案例驱动)。

背景: 原检测器对"单根 15m 破位后立即收复"的噪声也给信号(7/14 只),
而 AI 正确地把这类判为"区间内单根噪声而非确认的反转"。去噪规则:
  ① 破位/吞没类信号必须**放量**(≥1.15×近5根均量);
  ② 信号K线若已被反向收复(收盘越过前一根端点) → 失效。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pandas as pd
from supermarket.indicators import reversal_short


def _df(rows):
    """rows: [(open, high, low, close, volume)]"""
    return pd.DataFrame([{"ts": 1_790_000_000_000 + i * 900_000, "open": o, "high": h,
                          "low": l, "close": c, "volume": v} for i, (o, h, l, c, v) in enumerate(rows)])


def test_noise_single_breakout_without_volume_is_filtered():
    """单根缩量破位(无延续) → 不给顶部反转信号。"""
    rows = [(100, 101, 99.5, 100.5, 100)] * 8
    rows += [(100.5, 101, 99.0, 98.5, 60)]    # 缩量破位(量低于均值)
    d, desc = reversal_short(_df(rows), "15m")
    assert d == "", f"缩量破位应被过滤, 实际: {d} {desc}"


def test_volume_breakdown_is_signal():
    """放量跌破前 2 根低点 + 不再创新高 → 顶部反转信号。"""
    rows = [(100, 101.5, 99.8, 101.0, 100), (101, 102.0, 100.2, 101.5, 100),
            (101.5, 102.5, 100.5, 102.0, 100), (102, 103.0, 101.5, 102.5, 100),
            (102.5, 103.2, 102.0, 102.8, 100), (102.8, 103.0, 102.2, 102.4, 100),
            (102.4, 102.9, 101.9, 102.1, 100)]
    rows += [(102.1, 102.3, 100.3, 100.5, 300)]   # 放量跌破前2根低点(101.9/102.2)
    rows += [(100.5, 100.9, 100.1, 100.3, 120)]   # 收尾根(未收复) — 函数会剔除"进行中"的最后一根
    d, desc = reversal_short(_df(rows), "15m")
    assert d == "top", f"放量破位应给顶部信号, 实际: {d} {desc}"
    assert "放量" in desc


def test_bottom_reversal_needs_volume_or_long_wick():
    rows = [(103, 103.5, 102, 102.5, 100)] * 7
    rows += [(101, 101.2, 100.0, 100.2, 80)]      # 缩量下跌
    rows += [(100.2, 100.4, 99.8, 100.0, 80)]     # 缩量、无长下影、无收复
    d, _ = reversal_short(_df(rows), "15m")
    assert d != "bottom", "缩量且无长下影/收复 → 不应报底部反转"
