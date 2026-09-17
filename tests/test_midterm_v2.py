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
    assert "已废止" in SYSTEM_MANAGE
    assert "中期持有原则" in SYSTEM_MANAGE
    # 买入侧不再宣称薄利快周转
    assert "薄利快周转" not in SYSTEM_OPEN
    assert "4%~8%" in SYSTEM_OPEN


def test_hold_period_stated_as_days():
    """持仓期定位必须是'数日~数周'(与 A 方案一致)。"""
    from supermarket.prompts import SYSTEM_OPEN

    assert "数日~数周" in SYSTEM_OPEN
