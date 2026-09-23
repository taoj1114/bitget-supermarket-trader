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


def V(symbol="NVDAUSDT", price=220.0, side="long", sl=213.0, tp=235.0,
       contract=CONTRACT, acc=None, longs=0, shorts=0, leverage=None, margin_usd=None):
    """便捷调用: 返回 (ok, reason, params)。"""
    eng, a = make_engine()
    acc = acc or a
    return eng.validate_open(symbol, price, side, sl, tp, contract, acc, longs, shorts,
                             leverage=leverage, margin_usd=margin_usd)


# ---------- AI 决定杠杆(2026-09-19 用户要求: 按行情调节风险预算) ----------
def test_ai_low_leverage_shrinks_risk():
    """AI 给 8x → 名义 $16(风险减半), 保证金仍 $2。"""
    ok, reason, params = V(sl=213.0, tp=235.0, leverage=8)
    assert ok, reason
    assert params["leverage"] == 8
    assert 14.0 <= params["notional"] <= 17.0, params["notional"]


def test_ai_leverage_clamped_to_max():
    """AI 给 50x → 压到上限 20x(程序只钳制范围, 不替 AI 决策)。"""
    ok, reason, params = V(sl=213.0, tp=235.0, leverage=50)
    assert ok and params["leverage"] == 20


def test_ai_leverage_raised_to_min():
    """低于下限(1x) → 抬到 3x, 且名义仍满足交易所最低 $5。"""
    ok, reason, params = V(sl=213.0, tp=235.0, leverage=1)
    assert ok, reason
    assert params["leverage"] == 3
    assert params["notional"] >= 5


def test_leverage_absent_uses_default():
    """AI 未给 leverage → 用上限(=旧行为, 向后兼容)。"""
    ok, reason, params = V(sl=213.0, tp=235.0)
    assert ok and params["leverage"] == 20


def test_low_leverage_risk_budget_table():
    """风险预算表: 同一止损下, 杠杆越低单笔最大亏损越小(用户诉求的验证)。"""
    losses = {}
    for lev in (3, 8, 20):
        ok, _, p = V(sl=213.0, tp=233.0, leverage=lev)
        assert ok
        losses[lev] = p["notional"] * p["sl_dist_pct"] / 100
    assert losses[3] < losses[8] < losses[20]
    assert losses[20] / losses[3] > 3.0   # 高杠杆亏损是低杠杆的数倍


# ---------- 多头 ----------
def test_basic_ok():
    ok, reason, params = V(sl=213.0, tp=235.0, longs=0)
    assert ok, reason
    assert params["leverage"] == 20
    assert params["direction"] == "long"
    # $2 保证金 × 20x = $40 名义 / 220 ≈ 0.1818 → 步长0.01 → 0.18
    assert abs(params["notional"] - 39.6) < 1.0
    assert params["sl_dist_pct"] > 3.0 and params["rr"] >= 2.0


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
    # SL 3.18%, TP 2.27% → RR 0.71 → 拒绝
    ok, reason, _ = V(sl=213.0, tp=225.0)
    assert not ok and "盈亏比" in reason


def test_rr_marginally_low_auto_fix():
    """RR 1.0~2.0 临界: 自动修正 TP 至 2.0(保留 AI 意图), 而非拒单; RR<1.0 直接拒。"""
    # RR 0.71 (SL 3.18%, TP 2.27%) → 直接拒
    ok2, reason2, _ = V(sl=213.0, tp=225.0)
    assert not ok2 and "盈亏比" in reason2
    # RR 1.5 (SL 3.18%, TP 4.77%) → 通过(≥1.2, v3.1 薄利)
    ok3, reason3, params3 = V(sl=213.0, tp=230.5)
    assert ok3, reason3
    assert params3["rr"] >= 1.2
    assert abs(params3["tp_dist_pct"] - 4.77) < 0.25


def test_notional_cap_ok():
    _, acc = make_engine(equity=30.0, notional=120.0)  # 120+40 < 180 ✓
    ok, reason, _ = V(acc=acc, longs=1)
    assert ok


def test_notional_cap_reject():
    _, acc = make_engine(equity=30.0, notional=150.0)  # 150+40 > 180
    ok, reason, _ = V(acc=acc, longs=1)
    assert not ok and "名义超限" in reason


def test_position_cap():
    ok, reason, _ = V(longs=4)  # $30账户: 上限 min(6, floor(30×6/40))=4 → 满
    assert not ok and "多头仓数" in reason
    # 3 个多单时仍可开第4个
    ok, reason, _ = V(longs=3)
    assert ok


def test_position_cap_50usd():
    """$50 账户 → 上限 6(用户设定)。"""
    _, acc = make_engine(equity=50.0)
    ok, reason, _ = V(acc=acc, longs=5)
    assert ok, reason
    ok, reason, _ = V(acc=acc, longs=6)
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
    ok, reason, params = V(side="short", sl=228.0, tp=203.0)  # SL 3.6% / TP 7.7% → RR 2.1
    assert ok, reason
    assert params["direction"] == "short"
    assert params["sl_dist_pct"] > 3.0
    assert params["rr"] >= 2.0


def test_short_sl_below_price():
    # 空头止损必须在价格上方
    ok, reason, _ = V(side="short", sl=214.0, tp=200.0)
    assert not ok and "高于" in reason


def test_short_tp_above_price():
    # 空头止盈必须在价格下方
    ok, reason, _ = V(side="short", sl=228.0, tp=240.0)
    assert not ok and "止盈" in reason


def test_short_position_cap():
    ok, reason, _ = V(side="short", sl=228.0, tp=214.0, shorts=2)  # 上限2
    assert not ok and "空头仓数" in reason


def test_short_cap_not_block_long():
    # 空仓占满不影响多头
    ok, reason, _ = V(side="long", shorts=2)
    assert ok


# ---------- 宽止损通道(深跌票参考, 非开仓标准) ----------
def test_wide_sl_allowed():
    """短期v3.0 止损上限5%(用户2026-09-22): 6%拒, 5%过; TP上限10%(2026-09-23 用户: 止盈太高)。"""
    eng, acc = make_engine(equity=100.0)
    # SL 6% 拒绝(>5%)
    ok, reason, _ = eng.validate_open("XUSDT", 100.0, "long", 94.0, 108.0,
                                      CONTRACT, acc, 0, 0)
    assert not ok and "过远" in reason
    # 短期上限: 5% 止损默认可通过(RR 需 ≥1.5 → TP≥7.5%)
    ok, reason, params = eng.validate_open("XUSDT", 100.0, "long", 95.0, 108.0,
                                           CONTRACT, acc, 0, 0)
    assert ok, reason
    assert abs(params["sl_dist_pct"] - 5.0) < 0.01
    assert params["rr"] >= 1.2
    # TP 过高拒绝(>10%)
    ok2, reason2, _ = eng.validate_open("XUSDT", 100.0, "long", 95.0, 120.0,
                                        CONTRACT, acc, 0, 0)
    assert not ok2 and "止盈过高" in reason2


# ---------- 日线方向门控(镜像) ----------
def test_batch_limits():
    """分批建仓: 同一标的最多3批; 加仓不占用名额; 金字塔补货=只浮盈补。"""
    eng, acc = make_engine(equity=100.0)
    # 第1批(新开)通过
    ok, reason, params = eng.validate_open("NVDAUSDT", 100.0, "long", 95.0, 110.0, CONTRACT, acc, 5, 0)
    assert ok, reason
    # 第2/3批(加仓): 需现有浮盈≥0.5% + 新止损≥原均价+0.25% + SL≥2%距离, 通过
    ok, reason, _ = eng.validate_open("NVDAUSDT", 104.0, "long", 100.8, 110.5, CONTRACT, acc, 5, 0,
                                      batches_used=2, existing_pnl_pct=4.0, existing_entry=100.0)
    assert ok, reason
    # 第4批 → 拒(批次上限)
    ok, reason, _ = eng.validate_open("NVDAUSDT", 100.0, "long", 95.0, 110.0, CONTRACT, acc, 5, 0,
                                      batches_used=3, existing_pnl_pct=2.0, existing_entry=100.0)
    assert not ok and "批次" in reason
    # 浮亏补货 → 拒(不摊平)
    ok, reason, _ = eng.validate_open("NVDAUSDT", 99.0, "long", 95.0, 104.0, CONTRACT, acc, 5, 0,
                                      batches_used=1, existing_pnl_pct=-1.0, existing_entry=100.0)
    assert not ok and "浮盈" in reason
    # 空头镜像: 已2批再加 → 拒
    ok, reason, _ = eng.validate_open("NVDAUSDT", 100.0, "short", 105.0, 96.0, CONTRACT, acc, 0, 2,
                                      batches_used=3, existing_pnl_pct=2.0, existing_entry=100.0)
    assert not ok and "批次" in reason


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

# ---------- AI 决定仓位大小(2026-09-22 用户要求) ----------
def test_ai_margin_usd_scales_position():
    """AI 给 margin_usd=3.5 → 名义 = 3.5 × 杠杆(仓位大小可调)。"""
    ok, reason, params = V(sl=213.0, tp=235.0, leverage=10, margin_usd=3.5)
    assert ok, reason
    assert params["margin"] > 3.0 and params["notional"] > 30
    assert abs(params["notional"] - 35) < 3.0, params["notional"]  # 0.01股步长取整容忍


def test_ai_margin_clamped_range():
    """margin 钳制 [0.5, 5]: 给 0.1 → 0.5; 给 9 → 5; 不给 → 2(取整后 margin 只降不升)。"""
    def margin_of(m):
        ok, _, p = V(sl=213.0, tp=235.0, margin_usd=m)
        return p["margin"]
    assert abs(margin_of(0.1) - 0.5) < 0.1, margin_of(0.1)      # 下限 0.5
    assert margin_of(9.0) <= 5.0, margin_of(9.0)                # 上限 5
    assert margin_of(9.0) > 4.0, margin_of(9.0)
    assert abs(margin_of(None) - 2.0) < 0.1, margin_of(None)    # 缺省 2


def test_replenish_margin_not_heavier():
    """加仓(批次>0)时 AI 给的 margin 被压回首批标准(递减补货, 越加越轻)。"""
    from supermarket.config import Config
    from supermarket.risk import RiskEngine
    import tempfile
    cfg = Config()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(30.0)
        acc = {"equity": 30.0, "notional": 0.0, "position_count": 1,
               "long_count": 1, "short_count": 0}
        # 加仓场景: 浮盈4.5%(能补货) + SL抬到均价上方(补货即保护) + 距现价≥3%
        ok, reason, params = eng.validate_open(
            "NVDAUSDT", 230.0, "long", 222.6, 245.0, CONTRACT, acc, 1, 0,
            batches_used=1, leverage=10, margin_usd=4.0,
            existing_pnl_pct=4.5, existing_entry=220.0)
        assert ok, reason
        assert params["margin"] <= 2.1, params["margin"]   # 加仓不重于首批(base 2$, 0.01股取整容差)


def test_ai_max_positions_caps_total():
    """2026-09-22 用户: 最多开几个仓也由 AI 决定 — max_positions=总仓数上限,
    多头余量 = 上限 - 空头数。"""
    import tempfile
    from pathlib import Path
    from supermarket.config import Config
    from supermarket.risk import RiskEngine
    cfg = Config()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(50.0)
        acc = {"equity": 50.0, "notional": 60.0, "position_count": 2,
               "long_count": 2, "short_count": 0}
        # AI 上限=2, 已多2 → 拒第3个多
        ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, "long", 213.0, 235.0,
                                          CONTRACT, acc, 2, 0, max_positions_ai=2)
        assert not ok and "上限" in reason
        # AI 上限=4, 空1 → 多头余量 3; 已多2 → 允许再开
        acc2 = {"equity": 50.0, "notional": 60.0, "position_count": 3,
                "long_count": 2, "short_count": 1}
        ok2, reason2, _ = eng.validate_open("NVDAUSDT", 220.0, "long", 213.0, 235.0,
                                            CONTRACT, acc2, 2, 1, max_positions_ai=4)
        assert ok2, reason2


def test_ai_max_mult_controls_notional_cap():
    """2026-09-22 用户"充分利用资金": AI 决定总名义倍数(3~6), 程序只钳制不破 6。"""
    import tempfile
    from pathlib import Path
    from supermarket.config import Config
    from supermarket.risk import RiskEngine
    cfg = Config()
    with tempfile.TemporaryDirectory() as td:
        eng = RiskEngine(cfg, Path(td))
        eng.refresh_day(50.0)
        # AI 给 3x → 上限 150; 已用 120 + 新 40 = 160 > 150 → 拒
        acc = {"equity": 50.0, "notional": 120.0, "position_count": 2,
               "long_count": 2, "short_count": 0}
        ok, reason, _ = eng.validate_open("NVDAUSDT", 220.0, "long", 213.0, 235.0,
                                          CONTRACT, acc, 2, 0, max_mult_ai=3.0)
        assert not ok and "名义超限" in reason
        # AI 给 5x → 上限 250 → 160 < 250 → 允许
        ok2, reason2, _ = eng.validate_open("NVDAUSDT", 220.0, "long", 213.0, 235.0,
                                            CONTRACT, acc, 2, 0, max_mult_ai=5.0)
        assert ok2, reason2
