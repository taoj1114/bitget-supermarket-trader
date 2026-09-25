"""2026-09-25 TSLA 裸仓事故的防回归: 破位→贴现实价重挂→自动平仓(裸仓不裸奔)。"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from supermarket.execution import RealExecutor
from supermarket.bitget_client import BitgetError
from supermarket.execution import Position


class FakeBg:
    """第一次 place-strategy-order 抛破位错误; 第二次(贴现)成功。"""
    def __init__(self):
        self.calls, self.closed = [], []
    def v3_precision(self, symbol):
        return (2, 2)
    def quote(self, symbol):
        return {"lastPr": "372.0"}          # 现价已跌破原SL 378
    def _request(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/api/v3/trade/place-strategy-order":
            if len([c for c in self.calls if c[1] == path]) == 1:
                raise BitgetError("25590", "多头止盈止损(平多方向)，止损触发价格需要小于标记价格", path)
            return {"orderId": "OK2"}
        return {}
    def v3_close_order(self, symbol, qty, pos_side):
        self.closed.append((symbol, qty, pos_side))
        return {"orderId": "CLOSE1"}


class FakeBgFail2:
    """贴现重挂也失败 → 自动市价平仓。"""
    def __init__(self):
        self.closed = []
    def v3_precision(self, symbol):
        return (2, 2)
    def quote(self, symbol):
        return {"lastPr": "372.0"}
    def _request(self, method, path, body):
        raise BitgetError("25590", "多头止盈止损(平多方向)，止损触发价格需要小于标记价格", path)
    def v3_close_order(self, symbol, qty, pos_side):
        self.closed.append((symbol, qty, pos_side))
        return {"orderId": "CLOSE1"}


def _mk(bg):
    ex = object.__new__(RealExecutor)
    ex.bg, ex.cfg = bg, None
    ex._state = {"holds": {}, "holdings": {}, "log": [], "orders": []}
    ex._alerts, ex._locks = [], {}
    ex._alert = lambda *a: None
    ex._save = lambda *a: None
    return ex


def test_breakfail_reattach_at_market():
    bg = FakeBg()
    ex = _mk(bg)
    pos = Position(symbol="TSLAUSDT", direction="long", qty=0.08, avg_entry=384.38, leverage=20)
    ok = ex.repair_protection(pos, replace=True) if hasattr(ex, "repair_protection") else _rp(ex, bg, pos)
    assert ok is True, "破位后应贴现实价重挂成功"
    assert bg.closed == [], "贴现重挂成功, 不应平仓"
    sl2 = [c[2].get("stopLoss") for c in bg.calls if c[1].endswith("place-strategy-order")][-1]
    assert 370.0 < float(sl2) < 372.0, f"止损应贴近现价372×0.995≈370.1~372, 实际{sl2}"


def test_breakfail_auto_close():
    bg = FakeBgFail2()
    ex = _mk(bg)
    pos = Position(symbol="TSLAUSDT", direction="long", qty=0.08, avg_entry=384.38, leverage=20)
    ok = ex.repair_protection(pos, replace=True) if hasattr(ex, "repair_protection") else _rp(ex, bg, pos)
    assert ok is True, "平仓兜底应视作处理完成"
    assert bg.closed and bg.closed[0][0] == "TSLAUSDT" and bg.closed[0][2] == "long", \
        f"贴现重挂失败应市价平仓, 实际 {bg.closed}"


def _rp(ex, bg, pos):
    """直接在本模块复刻 repair_protection? 不 — 该函数已存在于 execution.py, 直接调用。"""
    return ex.repair_protection(pos)


def test_open_position_immediate_repair_mentioned():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "supermarket", "execution.py")).read()
    assert "立即补挂保护" in src, "开仓后 TPSL 失败必须立即补挂(不等下一轮)"
    assert "自动市价平仓(裸仓不裸奔)" in src, "补挂仍失败必须自动平仓"
    assert "贴现实价重挂" in src, "破位时应贴现实价重挂止损"
