"""Paper 执行层测试: 成交账目、费率、TPSL 穿价、资金费率结算。"""

import tempfile
from pathlib import Path

import pytest

from supermarket.config import Config
from supermarket.execution import PaperExecutor


@pytest.fixture
def pe():
    cfg = Config()
    cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        yield ex


def test_open_deducts_fee(pe):
    q = {"askPr": "200.00", "bidPr": "199.98", "lastPr": "200.00", "fundingRate": "0"}
    params = {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0}
    pos = pe.open("NVDAUSDT", params, q)
    notional = 0.2 * 200.0
    fee = notional * 0.0006
    assert pos.avg_entry == 200.0
    assert abs(pe._state["equity"] - (30.0 - fee)) < 1e-6
    assert pos.notional == 40.0


def test_tpsl_trigger_sl(pe):
    q = {"askPr": "200.00", "bidPr": "199.98", "lastPr": "200.00", "fundingRate": "0"}
    params = {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0}
    pe.open("NVDAUSDT", params, q)
    pe.set_quote("NVDAUSDT", {"lastPr": "194.5", "bidPr": "194.4", "askPr": "194.6", "fundingRate": "0"})
    pe.tick()
    assert "NVDAUSDT" not in pe._state["positions"]
    last = pe.closed_trades()[-1]
    assert last["reason"] == "SL_EXCHANGE"
    assert last["exit"] == 195.0  # 保守: 以 SL 价成交
    expect_pnl = (195.0 - 200.0) * 0.2 - 0.2 * 200.0 * 0.0006 - 0.2 * 195.0 * 0.0006
    assert abs(last["pnl"] - expect_pnl) < 1e-6


def test_tpsl_trigger_tp(pe):
    q = {"askPr": "200.00", "bidPr": "199.98", "lastPr": "200.00", "fundingRate": "0"}
    params = {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0}
    pe.open("NVDAUSDT", params, q)
    pe.set_quote("NVDAUSDT", {"lastPr": "211.0", "bidPr": "210.9", "askPr": "211.1", "fundingRate": "0"})
    pe.tick()
    assert "NVDAUSDT" not in pe._state["positions"]
    assert pe.closed_trades()[-1]["reason"] == "TP_EXCHANGE"


def test_ai_close(pe):
    q = {"askPr": "200.00", "bidPr": "199.98", "lastPr": "200.00", "fundingRate": "0"}
    pe.open("NVDAUSDT", {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0}, q)
    pe.set_quote("NVDAUSDT", {"lastPr": "202.0", "bidPr": "201.9", "askPr": "202.1", "fundingRate": "0"})
    res = pe.close("NVDAUSDT", reason="AI_CLOSE")
    assert res["ok"]
    trade = pe.closed_trades()[-1]
    assert trade["reason"] == "AI_CLOSE"
    expect = (201.9 - 200.0) * 0.2 - 0.2 * 200.0 * 0.0006 - 0.2 * 201.9 * 0.0006
    assert abs(trade["pnl"] - expect) < 1e-6


def test_funding_settlement(pe):
    """跨越 8h 边界 → 资金费率结算(多头付费)。"""
    q = {"askPr": "200.00", "bidPr": "199.98", "lastPr": "200.00", "fundingRate": "0.0001"}
    params = {"size": 0.2, "leverage": 20, "stop_loss": 100.0, "take_profit": 500.0}
    pe.open("NVDAUSDT", params, q)
    # 把 _last_settle 拨回到 9 小时前
    import time
    pe._state["positions"]["NVDAUSDT"]["_last_settle"] = time.time() - 9 * 3600
    pe.set_quote("NVDAUSDT", {"lastPr": "200.5", "bidPr": "200.4", "askPr": "200.6", "fundingRate": "0.0001"})
    pe.tick()
    # 40 名义 × 0.0001 = $0.004 付掉
    assert abs(pe._state["equity"] - (30.0 - 0.004 - 0.024)) < 1e-6


def test_batch_merge():
    """分批建仓合并: 同标的同方向两次开仓 → 合并数量、加权均价、批次=2。"""
    cfg = Config()
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        q = {"askPr": "100.0", "lastPr": "100.0"}
        ex.open("NVDAUSDT", {"size": "0.4", "leverage": 20, "stop_loss": 95.0,
                             "take_profit": 104.0, "volume_place": 4}, q)
        q2 = {"askPr": "90.0", "lastPr": "90.0"}
        ex.open("NVDAUSDT", {"size": "0.4", "leverage": 20, "stop_loss": 86.0,
                             "take_profit": 94.0, "volume_place": 4}, q2)
        p = [p for p in ex.positions() if p.symbol == "NVDAUSDT"][0]
        assert p.batches == 2
        assert abs(p.qty - 0.8) < 1e-9
        assert abs(p.avg_entry - 95.0) < 1e-9          # (100*0.4+90*0.4)/0.8
        assert p.sl == 86.0 and p.tp == 94.0            # 新批次 SL/TP 生效
        # 关闭时按合并后总量结算(平仓价90 < 均价95 → 亏损)
        pnl, _ = ex.close("NVDAUSDT", price=90.0)
        assert pnl < 0


def test_reload_persist():
    cfg = Config()
    cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        ex.open("NVDAUSDT", {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0},
                {"askPr": "200.0", "bidPr": "199.98", "lastPr": "200.0", "fundingRate": "0"})
        ex2 = PaperExecutor(cfg, Path(td))
        assert "NVDAUSDT" in ex2._state["positions"]
        assert ex2._state["equity"] < 30.0


def test_account_includes_unrealized():
    """paper 账户展示净值含未实现浮盈(风控/AI 口径), 已实现口径不变。"""
    cfg = Config(); cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        params = {"size": 0.2, "leverage": 20, "stop_loss": 195.0, "take_profit": 210.0,
                  "direction": "long"}
        ex.open("NVDAUSDT", params, {"askPr": "200.0", "bidPr": "199.98", "lastPr": "200.0",
                                     "fundingRate": "0"})
        # 价格涨 2.5% → 浮盈 +$1.0
        ex.set_quote("NVDAUSDT", {"lastPr": "205.0", "bidPr": "204.9", "askPr": "205.1",
                                  "fundingRate": "0"})
        acc = ex.account()
        fee = 0.2 * 200.0 * 0.0006
        assert abs(acc["equity"] - (30.0 - fee + 1.0)) < 1e-6  # 含浮盈
        assert abs(acc["unrealized"] - 1.0) < 1e-6
        assert abs(ex._state["equity"] - (30.0 - fee)) < 1e-6  # 已实现口径不变


# ---------- 空头镜像 ----------
def test_short_open_fills_at_bid():
    cfg = Config(); cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        params = {"size": 0.2, "leverage": 20, "stop_loss": 205.0, "take_profit": 190.0,
                  "direction": "short"}
        pos = ex.open("NVDAUSDT", params, {"askPr": "200.10", "bidPr": "199.90",
                                           "lastPr": "200.0", "fundingRate": "0"})
        assert pos.direction == "short"
        assert pos.avg_entry == 199.90  # 空头以 bid 成交


def test_short_tpsl_trigger():
    """空头 SL 在价格上方触发。"""
    cfg = Config(); cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        params = {"size": 0.2, "leverage": 20, "stop_loss": 205.0, "take_profit": 190.0,
                  "direction": "short"}
        ex.open("NVDAUSDT", params, {"askPr": "200.1", "bidPr": "199.9", "lastPr": "200.0",
                                     "fundingRate": "0"})
        # 价格涨到 206 → 空头止损触发 @205
        ex.set_quote("NVDAUSDT", {"lastPr": "206.0", "bidPr": "205.9", "askPr": "206.1",
                                  "fundingRate": "0"})
        ex.tick()
        assert "NVDAUSDT" not in ex._state["positions"]
        trade = ex.closed_trades()[-1]
        assert trade["reason"] == "SL_EXCHANGE"
        assert trade["exit"] == 205.0
        assert trade["direction"] == "short"
        expect = (199.9 - 205.0) * 0.2 - 0.2 * 199.9 * 0.0006 - 0.2 * 205.0 * 0.0006
        assert abs(trade["pnl"] - expect) < 1e-6


def test_short_ai_close_positive_pnl():
    """空头价格下跌 → 盈利兑现。"""
    cfg = Config(); cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        params = {"size": 0.2, "leverage": 20, "stop_loss": 210.0, "take_profit": 180.0,
                  "direction": "short"}
        ex.open("NVDAUSDT", params, {"askPr": "200.1", "bidPr": "199.9", "lastPr": "200.0",
                                     "fundingRate": "0"})
        # 多头平仓以 ask 成交(空头买回)
        ex.set_quote("NVDAUSDT", {"lastPr": "197.0", "bidPr": "196.9", "askPr": "197.1",
                                  "fundingRate": "0"})
        res = ex.close("NVDAUSDT", reason="AI_CLOSE")
        assert res["ok"]
        trade = ex.closed_trades()[-1]
        assert trade["reason"] == "AI_CLOSE"
        expect = (199.9 - 197.1) * 0.2 - 0.2 * 199.9 * 0.0006 - 0.2 * 197.1 * 0.0006
        assert abs(trade["pnl"] - expect) < 1e-6


def test_short_funding_received():
    """正费率时空头收取资金(镜像: 多头付费)。"""
    cfg = Config(); cfg.paper.initial_equity = 30.0
    with tempfile.TemporaryDirectory() as td:
        ex = PaperExecutor(cfg, Path(td))
        params = {"size": 0.2, "leverage": 20, "stop_loss": 210.0, "take_profit": 180.0,
                  "direction": "short"}
        ex.open("NVDAUSDT", params, {"askPr": "200.1", "bidPr": "199.9", "lastPr": "200.0",
                                     "fundingRate": "0.0001"})
        import time
        ex._state["positions"]["NVDAUSDT"]["_last_settle"] = time.time() - 9 * 3600
        ex.set_quote("NVDAUSDT", {"lastPr": "200.5", "bidPr": "200.4", "askPr": "200.6",
                                  "fundingRate": "0.0001"})
        ex.tick()
        # 空头收 40×0.0001 = +$0.004; 开仓手续费 0.2×199.9×0.0006
        fee = 0.2 * 199.9 * 0.0006
        assert abs(ex._state["equity"] - (30.0 + 0.004 - fee)) < 1e-5