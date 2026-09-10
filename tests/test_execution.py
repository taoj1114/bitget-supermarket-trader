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