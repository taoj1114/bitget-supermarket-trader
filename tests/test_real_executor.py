"""RealExecutor 实盘路径测试(v3/统一账户 UTA 语义, 用假 bg 对象, 不触网)。

语义依据 2026-09 真金实测:
- 开仓: v3_set_leverage → v3_place_order(带 preset TP/SL, posSide=方向)
- 平仓: v3_close_order(hedge 模式只传 posSide, 不能带 reduceOnly → 25238)
- TPSL: 由交易所策略单维护, manage_tpsl 走 modify_strategy(止损只收紧不放宽)
- 对账: v3_last_closed_pnl(execPnl 真实盈亏)
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
    def __init__(self, has_position: bool = True):
        self.calls: list[tuple] = []
        self.has_position = has_position
        self._open = False
        self.strategy = [{"orderId": "strat-1", "symbol": "NVDAUSDT", "posSide": "long",
                          "stopLoss": "95", "takeProfit": "110", "qty": "1.0"}]

    # ---- v3 接口 ----
    def v3_account(self):
        self.calls.append(("v3_account",))
        return {"equity": 50.0, "available": 45.0, "unrealised": 0.0}

    def v3_precision(self, symbol):
        return (2, 2)

    def v3_set_leverage(self, symbol, leverage, margin_mode="crossed"):
        self.calls.append(("v3_set_leverage", symbol, leverage, margin_mode))
        return {}

    def v3_place_order(self, symbol, side, qty, pos_side="long", order_type="market",
                       price=None, stop_loss=None, take_profit=None, client_oid=None):
        self.calls.append(("v3_place_order", symbol, side, qty, pos_side,
                           stop_loss, take_profit))
        self._open = True
        return {"orderId": "oid-1"}

    def v3_close_order(self, symbol, qty, pos_side):
        self.calls.append(("v3_close_order", symbol, qty, pos_side))
        self._open = False
        return {"orderId": "close-1"}

    def v3_positions(self):
        if not (self._open and self.has_position):
            return []
        return [{"symbol": "NVDAUSDT", "total": "1.0", "avgPrice": "100.0",
                 "posSide": "long", "leverage": "20", "createdTime": "1700000000000"}]

    def v3_strategy_orders(self, symbol=None):
        return list(self.strategy)

    def v3_cancel_strategy(self, symbol, order_id):
        self.calls.append(("v3_cancel_strategy", symbol, order_id))
        self.strategy = []
        return {}

    def v3_modify_strategy(self, symbol, order_id, stop_loss=None, take_profit=None):
        self.calls.append(("v3_modify_strategy", symbol, order_id, stop_loss, take_profit))
        return {}

    def v3_last_closed_pnl(self, symbol):
        self.calls.append(("v3_last_closed_pnl", symbol))
        return 0.42

    def quote(self, symbol):
        return {"lastPr": "100.0"}


def make_exec(has_position=True):
    cfg = Config()
    td = tempfile.TemporaryDirectory()
    bg = FakeBG(has_position=has_position)
    return RealExecutor(bg, cfg, Path(td.name)), bg, td


PARAMS = {"size": 1.0, "leverage": 20, "stop_loss": 95.0, "take_profit": 110.0,
          "direction": "long"}


def test_interface_complete():
    ex, _bg, _td = make_exec()
    for m in ("account", "positions", "set_quote", "tick", "open", "close",
              "manage_tpsl", "closed_trades", "closed_pnl"):
        assert callable(getattr(ex, m)), f"缺接口: {m}"


def test_open_flow_with_preset_tpsl():
    """开仓顺序: set_leverage → place_order(带 SL/TP) → 回读持仓 → 记录批次。"""
    ex, bg, _td = make_exec()
    ex.set_quote("NVDAUSDT", {"lastPr": "100.0"})
    pos = ex.open("NVDAUSDT", PARAMS, {"lastPr": "100.0"})
    kinds = [c[0] for c in bg.calls]
    assert kinds[0] == "v3_set_leverage" and kinds[1] == "v3_place_order", kinds
    order = [c for c in bg.calls if c[0] == "v3_place_order"][0]
    assert order[2] == "buy" and order[4] == "long"          # side/posSide
    assert order[5] == 95.0 and order[6] == 110.0            # preset SL/TP 随单
    assert pos.avg_entry == 100.0
    h = ex._state["holdings"]["NVDAUSDT"]
    assert h["batches"] == 1 and h["sl"] == 95.0 and h["tp"] == 110.0
    assert h["tpsl_ids"] == ["strat-1"]                      # 策略单已确认


def test_open_short_uses_short_pos_side():
    ex, bg, _td = make_exec()
    bg.v3_positions = lambda: ([{"symbol": "NVDAUSDT", "total": "1.0", "avgPrice": "100.0",
                                 "posSide": "short", "leverage": "20",
                                 "createdTime": "1700000000000"}] if bg._open else [])
    ex.open("NVDAUSDT", dict(PARAMS, direction="short", stop_loss=105.0, take_profit=90.0),
            {"lastPr": "100.0"})
    order = [c for c in bg.calls if c[0] == "v3_place_order"][0]
    assert order[2] == "sell" and order[4] == "short", order


def test_open_reread_failure_raises():
    """开仓后回读不到持仓 → 抛错交人工核对(不静默)。"""
    ex, bg, _td = make_exec(has_position=False)
    with pytest.raises(RuntimeError):
        ex.open("NVDAUSDT", PARAMS, {"lastPr": "100.0"})


def test_close_uses_v3_close_order_and_real_pnl():
    """平仓走 v3_close_order(hedge 语义) + fills 真实盈亏对账。"""
    ex, bg, _td = make_exec()
    ex._state["holdings"]["NVDAUSDT"] = {"sl": 95.0, "tp": 110.0, "batches": 1,
                                         "direction": "long"}
    bg._open = True
    res = ex.close("NVDAUSDT", reason="AI_CLOSE")
    assert res["ok"] is True and abs(res["pnl"] - 0.42) < 1e-9
    co = [c for c in bg.calls if c[0] == "v3_close_order"][0]
    assert co[3] == "long", co                                  # 只传 posSide
    assert not any(c[0] == "v3_place_order" for c in bg.calls)  # 不再用 v2 下单
    assert "NVDAUSDT" not in ex._state.get("holdings", {})


def test_manage_tpsl_only_tightens():
    """管仓: 止损只收紧不放宽(多头不上移放宽); 差异小不动。"""
    ex, bg, _td = make_exec()
    bg._open = True
    ex._state["holdings"]["NVDAUSDT"] = {"sl": 95.0, "tp": 110.0, "batches": 1,
                                         "direction": "long"}
    # SL 想放宽到 90(< 当前95) → 忽略; TP 改到 120 → 生效
    ex.manage_tpsl("NVDAUSDT", 90.0, 120.0)
    mods = [c for c in bg.calls if c[0] == "v3_modify_strategy"]
    assert mods, "应调用 modify_strategy"
    assert mods[0][3] is None and mods[0][4] == 120.0, mods
    # 差异极小(相对现价0.2%阈值) → 不调用
    bg.calls.clear()
    ex.manage_tpsl("NVDAUSDT", 95.05, 110.0)
    assert not [c for c in bg.calls if c[0] == "v3_modify_strategy"]


def test_positions_fill_precision_and_batches():
    ex, bg, _td = make_exec()
    bg._open = True
    ex._state["holdings"]["NVDAUSDT"] = {"sl": 95.0, "tp": 110.0, "batches": 2,
                                         "direction": "long"}
    p = ex.positions()[0]
    assert p.volume_place == 2 and p.batches == 2 and p.sl == 95.0


# ---------- 健壮性: 保护自愈 / 平仓重试 / 告警 ----------

def test_unprotected_position_detected_and_repaired():
    """裸仓检测 + 自动补挂 TPSL(实盘安全网)。"""
    ex, bg, _td = make_exec()
    bg._open = True
    bg.strategy = []                       # 模拟策略单丢失 → 裸仓
    bare = ex.unprotected_positions()
    assert len(bare) == 1 and bare[0].symbol == "NVDAUSDT"

    calls = []

    def fake_request(method, path, body=None, **kw):
        calls.append((method, path, body))
        return {"orderId": "repaired-1"}

    bg._request = fake_request
    ok = ex.repair_protection(bare[0])
    assert ok is True
    placement = [c for c in calls if "place-strategy-order" in c[1]]
    assert placement, "应调用 place-strategy-order"
    body = placement[0][2]
    assert body["type"] == "tpsl", body          # 实测枚举: tpsl
    assert body["posSide"] == "long" and float(body["stopLoss"]) > 0
    # 告警落盘
    assert any(a["kind"] == "PROTECTION_REPAIRED" for a in ex._state.get("alerts", []))


def test_protected_position_not_flagged():
    ex, bg, _td = make_exec()
    bg._open = True
    assert ex.unprotected_positions() == []      # 有策略单 → 不报裸仓


def test_close_retries_then_succeeds():
    """平仓前两次失败, 第三次成功(实盘: 网络抖动不应丢平仓)。"""
    ex, bg, _td = make_exec()
    bg._open = True
    attempts = {"n": 0}

    def flaky_close(symbol, qty, pos_side):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("network timeout")
        bg._open = False
        return {"orderId": "close-ok"}

    bg.v3_close_order = flaky_close
    res = ex.close("NVDAUSDT", reason="AI_CLOSE")
    assert res["ok"] is True and attempts["n"] == 3


def test_close_failure_alerts():
    """平仓3次全失败 → 告警落盘 + 返回失败(下轮重试)。"""
    ex, bg, _td = make_exec()
    bg._open = True

    def always_fail(symbol, qty, pos_side):
        raise RuntimeError("exchange down")

    bg.v3_close_order = always_fail
    res = ex.close("NVDAUSDT", reason="AI_CLOSE")
    assert res["ok"] is False
    assert any(a["kind"] == "CLOSE_FAIL" for a in ex._state.get("alerts", []))
