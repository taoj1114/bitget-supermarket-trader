"""风控矩阵测试: 代码硬约束逐条验证。"""

import tempfile
from pathlib import Path

from supermarket.config import Config
from supermarket.risk import RiskEngine


def make_cfg(**over):
    cfg = Config()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def make_engine(equity=30.0, notional=0.0, npos=0):
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(equity)
        return eng, {"equity": equity, "notional": notional, "position_count": npos}


CONTRACT = {"maxLever": 20, "sizeMultiplier": 0.01, "minTradeUSDT": 5}


def test_basic_ok():
    eng, acc = make_engine(equity=30.0)
    ok, reason, params = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0,
                                           CONTRACT, acc, 0)
    assert ok, reason
    assert params["leverage"] == 20
    # $2 保证金 × 20x = $40 名义 / 220 ≈ 0.1818 → 步长0.01 → 0.18
    assert abs(params["notional"] - 39.6) < 1.0
    assert params["sl_dist_pct"] > 2.5 and params["rr"] >= 1.5


def test_sl_missing():
    eng, acc = make_engine()
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, None, 231.0, CONTRACT, acc, 0)
    assert not ok and "止损" in reason


def test_tp_missing():
    eng, acc = make_engine()
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, None, CONTRACT, acc, 0)
    assert not ok and "止盈" in reason


def test_sl_above_price():
    eng, acc = make_engine()
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 226.0, 231.0, CONTRACT, acc, 0)
    assert not ok and "低于" in reason


def test_sl_too_close():
    eng, acc = make_engine()
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 219.0, 231.0, CONTRACT, acc, 0)  # 0.45%
    assert not ok and "过近" in reason


def test_sl_too_far():
    eng, acc = make_engine()
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 180.0, 258.0, CONTRACT, acc, 0)  # 18%
    assert not ok and "过远" in reason


def test_rr_too_low():
    eng, acc = make_engine()
    # SL 2.7%, TP 1.4% → RR 0.52
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 223.0,
                                      CONTRACT, acc, 0)
    assert not ok and "盈亏比" in reason


def test_notional_cap():
    eng, acc = make_engine(equity=30.0, notional=120.0)  # 已用$120 = 净值×4
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0, CONTRACT, acc, 1)
    # 120 + 40 > 30×6=180? 不, 120+40=160 < 180 → ok
    assert ok


def test_notional_cap_2():
    eng, acc = make_engine(equity=30.0, notional=150.0)  # 150+40=190 > 180
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0, CONTRACT, acc, 1)
    assert not ok and "名义超限" in reason


def test_position_cap():
    eng, acc = make_engine(equity=30.0, npos=3)  # 上限 max(1, 30//10)=3 → 已满
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0, CONTRACT, acc, 3)
    assert not ok and "仓数" in reason


def test_zero_equity():
    eng, acc = make_engine(equity=0.0)
    ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0, CONTRACT, acc, 0)
    assert not ok


def test_leverage_capped_by_contract():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(100.0)
        acc = {"equity": 100.0, "notional": 0.0, "position_count": 0}
        # AAOI maxLever=20 → 20x; 若某合约 maxLever=10 → 10x
        ok, reason, params = eng.validate_open("XUSDT", 50.0, 48.0, 55.0,
                                               {"maxLever": 10, "sizeMultiplier": 0.01,
                                                "minTradeUSDT": 5}, acc, 0)
        assert ok
        assert params["leverage"] == 10


def test_consecutive_loss_pause():
    cfg = make_cfg(max_consecutive_losses=2)
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(100.0)
        eng.on_close(-1.0)
        eng.on_close(-1.0)
        assert eng.paused() != ""
        ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, 214.0, 231.0,
                                          CONTRACT, {"equity": 100.0, "notional": 0,
                                                     "position_count": 0}, 0)
        assert not ok and "熔断" in reason


def test_win_resets_streak():
    cfg = make_cfg(max_consecutive_losses=2)
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(100.0)
        eng.on_close(-1.0)
        eng.on_close(-1.0)  # 触发暂停
        eng.state.paused_until = 0  # 模拟暂停结束
        eng.on_close(0.5)
        assert eng.state.consecutive_losses == 0
        assert eng.paused() == ""


def test_daily_direction_gate():
    eng, _ = make_engine()
    # 日线明确向下(ADX≥25) → 禁做多
    ok, reason = eng.validate_daily_direction("trend_down", 30.0)
    assert not ok and "逆势" in reason
    # 日线向下但 ADX 不足(衰竭期) → 允许(留给 AI 判断底部反转)
    ok, _ = eng.validate_daily_direction("trend_down", 15.0)
    assert ok
    # 日线向上/横盘 → 允许
    assert eng.validate_daily_direction("trend_up", 30.0)[0]
    assert eng.validate_daily_direction("flat", 10.0)[0]