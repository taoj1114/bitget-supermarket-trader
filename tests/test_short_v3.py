"""短期策略 v3.0 参数与提示词一致性测试(2026-09-22 用户选择: 转短期)。

背景: v2.0(中期, 止损3~15%/TP 10~15%/持有数周)与实盘行为(盈利单中位7.4h)错配;
19笔实盘: <24h持仓胜率64%合计+$0.37, 亏钱来自持仓拖久(且多为周末休市被动锁仓)。
用户决定: 短期化 — 紧止损1.5~5%/快兑现(3~5%目标)/时间止损48h。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.config import Config


def test_short_risk_params():
    """短期参数: 止损 1.5%~5%, 盈亏比 1.5, 时间止损 48h。"""
    c = Config.load()
    assert c.sl_min_pct == 1.5, "短期止损下限应为 1.5%"
    assert c.sl_max_pct == 5.0, "短期止损上限应为 5%"
    assert c.min_rr == 1.0, "短期盈亏比应 ≥1.0(开仓挂1.5~2%止盈止损, 靠胜率)"
    assert c.time_stop_hours == 48.0, "时间止损应 48h"


def test_short_prompt_consistency():
    """提示词与短期参数一致: TP 5~8%, 止损 1.5~5%, 时间止损, 无中期残留。"""
    from supermarket.prompts import SYSTEM_MANAGE, SYSTEM_OPEN
    assert "动量驱动" in SYSTEM_MANAGE and "乏力" in SYSTEM_MANAGE and "66" in SYSTEM_MANAGE
    assert "时间止损" in SYSTEM_MANAGE
    assert "5%~8%" in SYSTEM_OPEN or "3~5%" in SYSTEM_OPEN
    assert "1.5%~5%" in SYSTEM_OPEN
    assert "数日~数周" not in SYSTEM_OPEN, "短期策略不应再宣称中期持仓"
    assert "14天" not in SYSTEM_OPEN and "14天" not in SYSTEM_MANAGE, "中期滞销评估应移除"


def test_time_stop_backstop_in_engine():
    """时间止损程序兜底(短期 v3.0): engine 有 TIME_STOP 强制平仓。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "TIME_STOP" in src, "时间止损平仓应有独立原因标记"
    assert "时间止损" in src


def test_dynamic_intervals():
    """盘中提速(2026-09 用户要求): 盘中 5 分钟管仓, 开仓每 3 轮(15 分钟)。"""
    c = Config.load()
    assert c.interval_regular == 300
    assert c.interval_prepost == 900 and c.interval_closed == 1800
    assert c.scan_open_every == 3
    assert c.interval_for_session("weekend") == 1800


def test_profit_guard_ladder_rules():
    """利润保护阶梯: <4% 不动, ≥4% 保本, ≥6% 锁 3%(只升不降)。"""
    from supermarket.prompts import SYSTEM_MANAGE
    assert "利润保护阶梯" in SYSTEM_MANAGE
    assert "成本+0.5%" in SYSTEM_MANAGE and "成本+3%" in SYSTEM_MANAGE
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "prompts.py").read_text()
    mi = src.index("def build_manage_prompt")
    assert "利润保护阶梯" in src[mi:]


def test_scan_layering():
    """分层节拍: 管仓每轮跑, 开仓按 scan_open_every。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "def run_once(self, do_scan" in src
    assert "round_no % max(1, cfg.scan_open_every)" in src


def test_momentum_guard_backstop():
    """程序兜底(用户批准 2026-09-18): 动量乏力 + 浮盈≥1.5% → 程序直接兑现。"""
    c = Config.load()
    assert c.momentum_exit_floor == 1.5
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "MOMENTUM_GUARD" in src


def test_manage_filters_exchange_held():
    """2026-09-22 体检改进: 管仓只操作交易所实际持仓(手动平仓后不残留管仓)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "管仓持仓过滤" in src, "缺管仓持仓存在性过滤"
    assert "positions = [p for p in positions if p.symbol in held]" in src
