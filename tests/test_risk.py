"""风控矩阵测试: 代码硬约束逐条验证(多头/空头镜像)。"""

import tempfile
from pathlib import Path

from supermarket.config import Config
from supermarket.risk import RiskEngine


def make_cfg(**over):
    cfg = Config()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def make_engine(equity=30.0, notional=0.0, longs=0, shorts=0):
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(equity)
        return eng, {"equity": equity, "notional": notional,
                     "position_count": longs + shorts, "long_count": longs,
                     "short_count": shorts}


CONTRACT = {"maxLever": 20, "sizeMultiplier": 0.01, "minTradeUSDT": 5, "volumePlace": 4}


def V(symbol="NVDAUSDT", price=220.0, side="long", sl=214.0, tp=231.0,
       contract=CONTRACT, acc=None, longs=0, shorts=0):
    """便捷调用: 返回 (ok, reason, params)。"""
    eng, a = make_engine()
    acc = acc or a
    return eng.validate_open(symbol, price, side, sl, tp, contract, acc, longs, shorts)


# ---------- 多头 ----------
def test_basic_ok():
    ok, reason, params = V(sl=214.0, tp=231.0, longs=0)
    assert ok, reason
    assert params["leverage"] == 20
    assert params["direction"] == "long"
    # $2 保证金 × 20x = $40 名义 / 220 ≈ 0.1818 → 步长0.01 → 0.18
    assert abs(params["notional"] - 39.6) < 1.0
    assert params["sl_dist_pct"] > 2.5 and params["rr"] >= 1.5


def test_sl_missing():
    ok, reason, _ = V(sl=None)
    assert not ok and "止损" in reason


def test_tp_missing():
    ok, reason, _ = V(tp=None)
    assert not ok and "止盈" in reason


def test_sl_above_price():
    ok, reason, _ = V(sl=226.0)
    assert not ok and "低于" in reason


def test_sl_too_close():
    ok, reason, _ = V(sl=219.0, tp=231.0)  # 0.45%
    assert not ok and "过近" in reason


def test_sl_too_far():
    ok, reason, _ = V(sl=180.0, tp=258.0)  # 18%
    assert not ok and "过远" in reason


def test_rr_too_low():
    # SL 2.7%, TP 1.4% → RR 0.52 → 拒绝
    ok, reason, _ = V(sl=214.0, tp=223.0)
    assert not ok and "盈亏比" in reason


def test_rr_marginally_low_auto_fix():
    """RR 1.3~1.5 临界: 自动修正 TP 至 1.5(保留 AI 意图), 而非拒单。"""
    ok, reason, params = V(sl=214.0, tp=229.5)  # SL 2.7% / TP 4.3% = RR 1.58? 需要构造 1.3
    # 用精确构造: sl=214 (2.727%), tp=224 (1.818%) → RR 0.667 < 1.0 → 拒
    ok2, reason2, _ = V(sl=214.0, tp=224.0)
    assert not ok2 and "盈亏比" in reason2
    # RR 1.33 (SL 3%, TP 4%): sl=213.4 (3.0%), tp=228.8 (4.0%) → RR 1.33 → 自动修正
    ok3, reason3, params3 = V(sl=213.4, tp=228.8)
    assert ok3, reason3
    # 修正后 TP 提升至 RR=1.5: SL 3.0% → TP 距离 4.5%
    assert params3["rr"] == 1.5
    assert abs(params3["tp_dist_pct"] - 4.5) < 0.2


def test_notional_cap_ok():
    _, acc = make_engine(equity=30.0, notional=120.0)  # 120+40 < 180 ✓
    ok, reason, _ = V(acc=acc, longs=1)
    assert ok


def test_notional_cap_reject():
    _, acc = make_engine(equity=30.0, notional=150.0)  # 150+40 > 180
    ok, reason, _ = V(acc=acc, longs=1)
    assert not ok and "名义超限" in reason


def test_position_cap():
    ok, reason, _ = V(longs=3)  # 上限 max(1, 30//10)=3 → 满
    assert not ok and "多头仓数" in reason


def test_zero_equity():
    _, acc = make_engine(equity=0.0)
    ok, reason, _ = V(acc=acc)
    assert not ok


def test_leverage_capped_by_contract():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(100.0)
        acc = {"equity": 100.0, "notional": 0.0, "position_count": 0,
               "long_count": 0, "short_count": 0}
        ok, reason, params = eng.validate_open(
            "XUSDT", 50.0, "long", 48.0, 55.0,
            {"maxLever": 10, "sizeMultiplier": 0.01, "minTradeUSDT": 5, "volumePlace": 4},
            acc, 0, 0)
        assert ok
        assert params["leverage"] == 10


# ---------- 空头(镜像) ----------
def test_short_basic_ok():
    ok, reason, params = V(side="short", sl=226.0, tp=205.0)  # SL 2.7% / TP 6.8% → RR 2.5
    assert ok, reason
    assert params["direction"] == "short"
    assert params["sl_dist_pct"] > 2.5
    assert params["rr"] >= 1.5


def test_short_sl_below_price():
    # 空头止损必须在价格上方
    ok, reason, _ = V(side="short", sl=214.0, tp=200.0)
    assert not ok and "高于" in reason


def test_short_tp_above_price():
    # 空头止盈必须在价格下方
    ok, reason, _ = V(side="short", sl=226.0, tp=240.0)
    assert not ok and "止盈" in reason


def test_short_position_cap():
    ok, reason, _ = V(side="short", sl=226.0, tp=214.0, shorts=2)  # 上限2
    assert not ok and "空头仓数" in reason


def test_short_cap_not_block_long():
    # 空仓占满不影响多头
    ok, reason, _ = V(side="long", shorts=2)
    assert ok


# ---------- 宽止损通道(深跌票参考, 非开仓标准) ----------
def test_wide_sl_allowed():
    """全局上限15%; 深跌票宽通道上限20%(仅风控容忍, 不由程序触发)。"""
    eng, acc = make_engine(equity=100.0)
    # 默认: SL 18% 拒绝(>15%)
    ok, reason, _ = eng.validate_open("XUSDT", 100.0, "long", 82.0, 160.0,
                                      CONTRACT, acc, 0, 0)
    assert not ok and "过远" in reason
    # 宽通道: SL 18% 放行(≤20%)
    ok, reason, params = eng.validate_open("XUSDT", 100.0, "long", 82.0, 160.0,
                                           CONTRACT, acc, 0, 0, allow_wide_sl=True)
    assert ok, reason
    assert params["sl_dist_pct"] == 18.0
    assert params["rr"] >= 1.5
    # 超20%仍拒(失控底线)
    ok, reason, _ = eng.validate_open("XUSDT", 100.0, "long", 75.0, 175.0,
                                      CONTRACT, acc, 0, 0, allow_wide_sl=True)
    assert not ok and "过远" in reason
    # 用户容忍度: 15%止损现在默认就可通过
    ok, reason, params = eng.validate_open("XUSDT", 100.0, "long", 85.0, 160.0,
                                           CONTRACT, acc, 0, 0)
    assert ok, reason


# ---------- 日线方向门控(镜像) ----------
def test_daily_direction_gate():
    eng, _ = make_engine()
    # 日线明确向下(ADX≥25) → 禁做多
    ok, reason = eng.validate_daily_direction("trend_down", 30.0, "long")
    assert not ok and "逆势" in reason
    # 日线明确向上(ADX≥25) → 禁做空
    ok, reason = eng.validate_daily_direction("trend_up", 30.0, "short")
    assert not ok and "逆势" in reason
    # 日线向下但 ADX 不足(衰竭期) → 多头允许(留给 AI 判断底部反转)
    assert eng.validate_daily_direction("trend_down", 15.0, "long")[0]
    # 日线向上但 ADX 不足(衰竭期) → 空头允许(顶部反转开口)
    assert eng.validate_daily_direction("trend_up", 15.0, "short")[0]
    # 顺势方向放行
    assert eng.validate_daily_direction("trend_up", 30.0, "long")[0]
    assert eng.validate_daily_direction("trend_down", 30.0, "short")[0]
    assert eng.validate_daily_direction("flat", 10.0, "long")[0]
    assert eng.validate_daily_direction("flat", 10.0, "short")[0]


# ---------- 熔断 ----------
def test_consecutive_loss_pause():
    cfg = make_cfg(max_consecutive_losses=2)
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(100.0)
        eng.on_close(-1.0)
        eng.on_close(-1.0)
        assert eng.paused() != ""
        ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, "long", 214.0, 231.0,
                                          CONTRACT, {"equity": 100.0, "notional": 0,
                                                     "long_count": 0, "short_count": 0}, 0, 0)
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