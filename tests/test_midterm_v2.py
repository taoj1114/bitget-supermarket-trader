"""中期策略 v2.0 参数与提示词一致性测试(防回归到旧的短炒规则)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.config import Config


def test_midterm_risk_params():
    """中期参数: 止损下限 3%, 盈亏比 2.0(旧 2.0/1.5 是紧止盈宽止损的负期望组合)。"""
    c = Config.load()
    assert c.sl_min_pct == 3.0, "中期止损下限应为 3%"
    assert c.min_rr == 2.0, "中期盈亏比应 ≥2.0"


def test_scalp_rules_removed_from_prompts():
    """旧的'卖出不亏就是赚/浮盈0.5%兑现'必须已从提示词移除(防回归短炒)。"""
    from supermarket.prompts import SYSTEM_MANAGE, SYSTEM_OPEN

    # 允许历史说明引用(已废止), 但禁止作为规则出现
    assert "浮盈≥0.5%(覆盖往返手续费" not in SYSTEM_MANAGE
    assert "即可兑现(CLOSE)落袋为安" not in SYSTEM_MANAGE
    # 2026-09: 卖出现为"动量驱动"(推进/滞涨/乏力), 不再是固定浮盈百分比
    assert "动量驱动" in SYSTEM_MANAGE
    assert "乏力" in SYSTEM_MANAGE and "66" in SYSTEM_MANAGE
    # 买入侧不再宣称薄利快周转
    assert "薄利快周转" not in SYSTEM_OPEN
    # 2026-09-18 实证: 止盈改为极端保护位(固定TP截断趋势利润, 均值差近5倍)
    assert "10%~15%" in SYSTEM_OPEN and "极端保护" in SYSTEM_OPEN


def test_hold_period_stated_as_days():
    """持仓期定位必须是'数日~数周'(与 A 方案一致)。"""
    from supermarket.prompts import SYSTEM_OPEN

    assert "数日~数周" in SYSTEM_OPEN


def test_dynamic_intervals():
    """盘中提速(2026-09 用户要求): 盘中 5 分钟管仓, 开仓每 3 轮(15 分钟)。"""
    c = Config.load()
    assert c.interval_regular == 300, "盘中应为 5 分钟"
    assert c.interval_prepost == 900 and c.interval_closed == 1800
    assert c.scan_open_every == 3
    assert c.interval_for_session("regular") == 300
    assert c.interval_for_session("pre_market") == 900
    assert c.interval_for_session("closed") == 1800
    assert c.interval_for_session("weekend") == 1800


def test_profit_guard_ladder_rules():
    """利润保护阶梯: <4% 不动, ≥4% 保本, ≥6% 锁 3%(只升不降)。"""
    from supermarket.prompts import SYSTEM_MANAGE

    assert "利润保护阶梯" in SYSTEM_MANAGE
    assert "成本+0.5%" in SYSTEM_MANAGE and "成本+3%" in SYSTEM_MANAGE
    assert "不动止损" in SYSTEM_MANAGE
    # 管仓 prompt 必须实际渲染出该行
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "prompts.py").read_text()
    mi = src.index("def build_manage_prompt")
    assert "利润保护阶梯" in src[mi:], "仅 build_manage_prompt 渲染保护阶梯(开仓prompt不应含)"


def test_scan_layering():
    """分层节拍: 管仓每轮跑, 开仓按 scan_open_every。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "def run_once(self, do_scan" in src
    assert "round_no % max(1, cfg.scan_open_every)" in src
    assert "interval_for_session" in src


def test_momentum_guard_backstop():
    """程序兜底(用户批准 2026-09-18): 动量乏力 + 浮盈≥1.5% → 程序直接兑现。"""
    c = Config.load()
    assert c.momentum_exit_floor == 1.5
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "动量兜底" in src, "缺程序兜底逻辑"
    assert "MOMENTUM_GUARD" in src, "兜底平仓应有独立原因标记(便于统计)"
    assert "momentum_state" in src
