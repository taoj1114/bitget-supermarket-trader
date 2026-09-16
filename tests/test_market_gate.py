"""大盘趋势门控 + 当日止损熔断收紧 测试(2026-09 用户实证后新增)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.config import Config


def test_market_down_adx_default():
    c = Config.load()
    assert c.market_down_adx == 25.0


def test_daily_drawdown_tightened():
    """当日亏损熔断从 30% 收紧到 5%(用户实证: 阴跌期反复认错会累积)。"""
    c = Config.load()
    assert c.max_daily_drawdown_pct == 5.0


def test_market_gate_blocks_long_only(tmp_path):
    """个股日线向下: 多单拦, 空单放行(空单是弱市对冲手段)。"""
    from supermarket.risk import RiskEngine

    c = Config.load()
    rm = RiskEngine(c, tmp_path)
    ok, reason = rm.validate_daily_direction("trend_down", 30.0, "long")
    assert not ok and "禁做多" in reason
    ok, _ = rm.validate_daily_direction("trend_down", 30.0, "short")
    assert ok


def test_market_down_flag_wiring():
    """engine 里应存在大盘门控标记与 SPY 日线检测(防误删)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "_market_down" in src and "daily_regime" in src
    assert "大盘日线向下" in src


def test_daily_regime_method_exists():
    """market.py 必须提供轻量日线 regime 方法(门控数据源)。"""
    from supermarket.market import MarketData
    assert hasattr(MarketData, "daily_regime")
