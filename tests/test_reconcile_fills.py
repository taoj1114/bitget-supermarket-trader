"""fills 权威对账逻辑测试(2026-09-21 新增; 纯逻辑, 不调交易所)。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reconcile_fills import scan_fills, audit


class FakeBG:
    def __init__(self, lst):
        self.lst = lst

    def _request(self, *a, **k):
        return {"list": self.lst}


def test_scan_fills_aggregates_same_second():
    """同标的同时刻多笔拆单合并为同一批平仓。"""
    lst = [
        {"symbol": "X", "tradeSide": "close_long", "createdTime": "1000000",
         "execQty": "0.02", "execPnl": "0.1", "execPrice": "10"},
        {"symbol": "X", "tradeSide": "close_long", "createdTime": "1000000",
         "execQty": "0.03", "execPnl": "0.2", "execPrice": "10"},
        {"symbol": "Y", "tradeSide": "close_long", "createdTime": "2000000",
         "execQty": "1", "execPnl": "0.5", "execPrice": "20"},
    ]
    f = scan_fills(FakeBG(lst), days=1)
    assert len(f) == 2
    bx = [x for x in f if x["symbol"] == "X"][0]
    assert bx["qty"] == 0.05 and abs(bx["pnl"] - 0.3) < 1e-9 and bx["n"] == 2


def test_scan_fills_since_filter():
    """启动时间过滤: 排除超市开业前的历史试运行成交。"""
    lst = [
        {"symbol": "OLD", "tradeSide": "close_long", "createdTime": "1000",
         "execQty": "1", "execPnl": "1", "execPrice": "1"},
        {"symbol": "NEW", "tradeSide": "close_long", "createdTime": "1789405000000",
         "execQty": "1", "execPnl": "2", "execPrice": "2"},
    ]
    f = scan_fills(FakeBG(lst), days=1, since_ms=4000000)
    assert [x["symbol"] for x in f] == ["NEW"]


def test_audit_ok_when_matching():
    """fills 与账本一致 → ok; 差异 → 检测到。"""
    fills = [{"symbol": "X", "tradeSide": "close_long", "createdTime": "1789405000000",
              "execQty": "0.1", "execPnl": "-0.6562", "execPrice": "229"}]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ai_memory.json").write_text(json.dumps({"decisions": [
            {"symbol": "X", "outcome": "closed", "pnl": -0.6562, "ts": 1789400000.0}]}))
        r = audit(FakeBG(fills), None, d, days=30)
        assert r["ok"], r["diffs"]
        # 账本 pnl 改错 → 检出
        (d / "ai_memory.json").write_text(json.dumps({"decisions": [
            {"symbol": "X", "outcome": "closed", "pnl": -0.30, "ts": 1789400000.0}]}))
        r2 = audit(FakeBG(fills), None, d, days=30)
        assert not r2["ok"] and r2["diffs"][0]["symbol"] == "X"


def test_audit_ignores_dust():
    """min_pnl 尘埃过滤(NVDA -0.0009 类试运行单)。"""
    fills = [{"symbol": "X", "tradeSide": "close_long", "createdTime": "1789405000000",
              "execQty": "1", "execPnl": "-0.0009", "execPrice": "1"}]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ai_memory.json").write_text(json.dumps({"decisions": []}))
        r = audit(FakeBG(fills), None, d, days=30)
        assert r["ok"], "尘埃单应被忽略(不产生差异)"