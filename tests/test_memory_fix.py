"""2026-09-21 体检修复: outcome 脏值判定 + pnl=None 防御。"""

import tempfile
from pathlib import Path

from supermarket.memory import AIMemory


def make_mem(recs: list[dict]) -> AIMemory:
    with tempfile.TemporaryDirectory() as td:
        mem = AIMemory(state_dir=Path(td))
        mem.decisions = recs
        return mem


def test_outcome_open_is_open_not_closed():
    """历史脏值 outcome='open'(恢复记录误标) 必须按开仓处理。"""
    recs = [
        {"symbol": "DDOGUSDT", "action": "BUY", "entry": 233.0, "outcome": "open"},   # 脏值
        {"symbol": "BABAUSDT", "action": "SELL", "entry": 112.0, "outcome": "closed", "pnl": -0.3},
        {"symbol": "TSLAUSDT", "action": "BUY", "entry": 362.0, "outcome": None},      # 正常开仓
    ]
    mem = make_mem(recs)
    opens = mem.open_decisions()
    closed = mem.closed_decisions()
    assert len(opens) == 2 and len(closed) == 1, (opens, closed)
    assert {d["symbol"] for d in opens} == {"DDOGUSDT", "TSLAUSDT"}


def test_stats_tolerates_pnl_none():
    """stats() 不得因 pnl=None 崩溃(快照路径, 20:10 实测崩溃)。"""
    recs = [{"symbol": "A", "action": "BUY", "entry": 1.0, "outcome": "closed",
             "pnl": None, "close_reason": "AI_CLOSE"}]
    mem = make_mem(recs)
    st = mem.stats()
    assert st["closed"] == 1 and st["wins"] == 0 and st["pnl"] == 0.0


def test_symbol_history_tolerates_pnl_none():
    """get_symbol_history 渲染 pnl=None 不得崩溃(管仓注入路径)。"""
    recs = [{"symbol": "A", "action": "BUY", "entry": 1.0, "outcome": "closed",
             "pnl": None, "ts": 1789400000, "reason": "x" * 60}]
    mem = make_mem(recs)
    h = mem.get_symbol_history("A")
    assert isinstance(h, str) and "pnl=$" in h
