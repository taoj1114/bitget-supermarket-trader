"""RealExecutor 实盘路径测试(用假 bg 对象, 不触网不花钱)。

覆盖: 接口完整性 / 下单顺序与 posSide / TPSL 失败回滚 / 平仓 posSide / 批次与SL记录
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from supermarket.config import Config
from supermarket.execution import RealExecutor


class FakeBG:
    """记录调用的假客户端。"""

    def __init__(self, tpsl_fail: bool = False, has_position: bool = True):
        self.calls: list[tuple] = []
        self.tpsl_fail = tpsl_fail
        self.has_position = has_position
        self._pos_open = False

    # ---- 被 RealExecutor 调用的接口 ----
    def set_leverage(self, symbol, leverage, hold_side="long"):
        self.calls.append(("set_leverage", symbol, leverage, hold_side))
        return {}

    def place_order(self, symbol, side, size, order_type="market", leverage=20,
                    margin_mode="crossed", reduce_only=False, pos_side="long"):
        self.calls.append(("place_order", symbol, side, size, order_type,
                           reduce_only, pos_side))
        if reduce_only:
            self._pos_open = False
        else:
            self._pos_open = True
        return {"orderId": "oid-123"}

    def positions(self):
        if not (self._pos_open and self.has_position):
            return []
        return [{"symbol": "NVDAUSDT", "holdVol": "1.0", "avgEntryPrice": "100.0",
                 "holdSide": "long", "leverage": "20", "openTimeAvg": "1700000000000"}]

    def place_tpsl(self, symbol, plan_type, trigger_price, execute_price=None, hold_side="long"):
        self.calls.append(("place_tpsl", symbol, plan_type, trigger_price, hold_side))
        if self.tpsl_fail:
            raise RuntimeError("[43023] 仓位不足")
        return {"orderId": f"tpsl-{plan_type}"}

    def cancel_plan(self, symbol, order_id):
        self.calls.append(("cancel_plan", symbol, order_id))
        return {}

    def pending_plans(self, symbol=None):
        return []

    def quote(self, symbol):
        return {"lastPr": "100.0"}

    def stock_contracts(self):
        return [{"symbol": "NVDAUSDT", "volumePlace": 2, "maxLever": 20,
                 "minTradeUSDT": 5, "sizeMultiplier": 1}]

    def account(self, symbol="NVDAUSDT"):
        return {"usdtEquity": "50.0", "available": "45.0"}

    def _request(self, *a, **kw):
        return {"fillList": []}

    def positions_all(self):
        return self.positions()


def make_exec(tpsl_fail=False):
    cfg = Config()
    td = tempfile.TemporaryDirectory()
    bg = FakeBG(tpsl_fail=tpsl_fail)
    return RealExecutor(bg, cfg, Path(td.name)), bg, td


PARAMS = {"size": 0.4, "leverage": 20, "stop_loss": 95.0, "take_profit": 110.0,
          "volume_place": 2, "direction": "long"}


def test_real_executor_interface_complete():
    """engine 调用的所有接口都必须存在(set_quote/tick 曾缺失→实盘首轮崩)。"""
    ex, _bg, _td = make_exec()
    for m in ("account", "positions", "set_quote", "tick", "open", "close",
              "manage_tpsl", "closed_trades"):
        assert callable(getattr(ex, m)), f"RealExecutor 缺接口: {m}"


def test_real_open_order_flow_and_pos_side():
    ex, bg, _td = make_exec()
    ex.set_quote("NVDAUSDT", {"lastPr": "100.0"})
    pos = ex.open("NVDAUSDT", PARAMS, {"lastPr": "100.0"})
    kinds = [c[0] for c in bg.calls]
    # 顺序: set_leverage → place_order(开仓) → place_tpsl×2
    assert kinds[0] == "set_leverage", kinds
    assert kinds[1] == "place_order", kinds
    assert kinds.count("place_tpsl") == 2, kinds
    # posSide 必须跟随方向(空头曾因硬编码 long 报错)
    order_meta = [c for c in bg.calls if c[0] == "place_order"][0]
    assert order_meta[6] == "long", order_meta
    assert pos.qty == 1.0
    # 本地记录: 批次/止损
    h = ex._state["holdings"]["NVDAUSDT"]
    assert h["batches"] == 1 and h["sl"] == 95.0 and h["tp"] == 110.0


def test_real_short_uses_short_pos_side():
    ex, bg, _td = make_exec()
    p = dict(PARAMS, direction="short", stop_loss=105.0, take_profit=90.0)
    # 空头: positions() 需返回 short 方向
    bg.positions = lambda: [{"symbol": "NVDAUSDT", "holdVol": "1.0",
                             "avgEntryPrice": "100.0", "holdSide": "short",
                             "leverage": "20", "openTimeAvg": "1700000000000"}] if bg._pos_open else []
    ex.open("NVDAUSDT", p, {"lastPr": "100.0"})
    lev_call = [c for c in bg.calls if c[0] == "set_leverage"][0]
    assert lev_call[3] == "short"
    order_call = [c for c in bg.calls if c[0] == "place_order"][0]
    assert order_call[2] == "sell" and order_call[6] == "short", order_call


def test_real_open_rollback_when_tpsl_fails():
    """TPSL 挂单失败(如43023) → 必须立即平仓回滚, 不留裸仓。"""
    ex, bg, _td = make_exec(tpsl_fail=True)
    with pytest.raises(RuntimeError):
        ex.open("NVDAUSDT", PARAMS, {"lastPr": "100.0"})
    orders = [c for c in bg.calls if c[0] == "place_order"]
    assert len(orders) == 2, "应有 开仓 + 回滚平仓 两笔"
    rollback = orders[1]
    assert rollback[5] is True and rollback[2] == "sell" and rollback[6] == "long", rollback
    # 回滚后无持仓记录
    assert "NVDAUSDT" not in ex._state.get("holdings", {})


def test_real_close_uses_pos_side_and_reduce_only():
    ex, bg, _td = make_exec()
    bg._pos_open = True
    res = ex.close("NVDAUSDT", reason="AI_CLOSE")
    assert res["ok"] is True
    order = [c for c in bg.calls if c[0] == "place_order"][0]
    assert order[2] == "sell" and order[5] is True and order[6] == "long", order
    # holdings 清理
    assert "NVDAUSDT" not in ex._state.get("holdings", {})


def test_real_positions_fall_back_batches_and_precision():
    """positions() 应回填 volume_place(合约精度) 与批次/止损(本地记录)。"""
    ex, bg, _td = make_exec()
    bg._pos_open = True
    ex._state["holdings"]["NVDAUSDT"] = {"sl": 95.0, "tp": 110.0, "batches": 2,
                                         "direction": "long", "volume_place": 2}
    pos = ex.positions()[0]
    assert pos.volume_place == 2        # 合约精度(非默认4, 否则平仓精度不符被拒)
    assert pos.batches == 2 and pos.sl == 95.0 and pos.tp == 110.0
