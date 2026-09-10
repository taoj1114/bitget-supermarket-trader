"""执行层: PaperExecutor / RealExecutor(同接口)。全仓模式。

RealExecutor 下单顺序(DETAILED_DESIGN §7): set_leverage → 先挂 TPSL(保护) → 市价开仓
→ 回读持仓核实 → 落盘。任何一步失败 → 撤已挂 TPSL + 拒绝。

PaperExecutor: 真实行情 ask/bid 成交 + 真实费率 + 资金费率 8h 结算 + TPSL 穿价模拟。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    leverage: int
    sl: float = 0.0
    tp: float = 0.0
    opened_ts: float = 0.0
    close_reason: str = ""
    funding_paid: float = 0.0
    open_fee: float = 0.0
    volume_place: int = 4
    direction: str = "long"   # long / short

    @property
    def notional(self) -> float:
        return self.qty * self.avg_entry

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in ("symbol", "qty", "avg_entry",
                                                                    "leverage", "sl", "tp",
                                                                    "opened_ts", "close_reason",
                                                                    "funding_paid", "open_fee",
                                                                    "volume_place", "direction")})


def _atomic_write(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


# ---------------------------------------------------------------------------
class PaperExecutor:
    """模拟账户: 真实行情成交 + 真实费率 + 资金费率 8h 结算 + TPSL 穿价。"""

    name = "paper"

    def __init__(self, cfg, state_dir: Path):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self._file = self.state_dir / "paper.json"
        self._state = self._load() or {
            "equity": cfg.paper.initial_equity,
            "positions": {},
            "closed": [],
            "realized_pnl": 0.0,
        }
        self._quote_cache: dict[str, dict] = {}

    def _load(self) -> dict | None:
        try:
            if self._file.exists():
                return json.loads(self._file.read_text())
        except Exception as e:
            log.warning("paper.json 读取失败: %s", str(e)[:80])
        return None

    def _save(self) -> None:
        _atomic_write(self._file, self._state)

    # ---- 对外接口 ----
    def account(self, symbol: str = "NVDAUSDT") -> dict[str, Any]:
        eq = float(self._state["equity"])
        pos = self._positions()
        # 未实现浮盈亏(按方向)计入展示净值/日回撤口径; state.equity 保持已实现口径
        unreal = 0.0
        for p in pos:
            q = self._quote_cache.get(p.symbol)
            if not q:
                continue
            last = float(q.get("lastPr", 0) or 0)
            if last <= 0:
                continue
            if p.direction == "long":
                unreal += (last - p.avg_entry) * p.qty
            else:
                unreal += (p.avg_entry - last) * p.qty
        eq_view = eq + unreal
        notional = sum(p.notional for p in pos)
        longs = sum(1 for p in pos if p.direction == "long")
        shorts = sum(1 for p in pos if p.direction == "short")
        return {"equity": eq_view, "available": eq - sum(p.notional / p.leverage for p in pos),
                "unrealized": unreal,
                "notional": notional, "position_count": len(pos), "long_count": longs,
                "short_count": shorts, "day_pnl": 0.0,
                "mode": "paper"}

    def positions(self) -> list[Position]:
        return self._positions()

    def set_quote(self, symbol: str, quote: dict[str, Any]) -> None:
        self._quote_cache[symbol] = quote

    def tick(self) -> None:
        """按最新 quote 计算浮盈 + 触发 TPSL(方向镜像) + 资金费率结算。"""
        for sym in list(self._state["positions"].keys()):
            q = self._quote_cache.get(sym)
            if not q:
                continue
            last = float(q.get("lastPr", 0) or 0)
            pos = self._positions_map().get(sym)
            if not pos or last <= 0:
                continue
            # TPSL 穿价(方向镜像; 保守: 均以触发价成交)
            if pos.direction == "long":
                if pos.sl and last <= pos.sl:
                    self.close(sym, reason="SL_EXCHANGE", price=pos.sl)
                    continue
                if pos.tp and last >= pos.tp:
                    self.close(sym, reason="TP_EXCHANGE", price=pos.tp)
                    continue
            else:
                if pos.sl and last >= pos.sl:
                    self.close(sym, reason="SL_EXCHANGE", price=pos.sl)
                    continue
                if pos.tp and last <= pos.tp:
                    self.close(sym, reason="TP_EXCHANGE", price=pos.tp)
                    continue
            # 资金费率结算(8h)
            self._settle_funding(pos, q)
        self._save()

    def _settle_funding(self, pos: Position, quote: dict, now: float | None = None) -> None:
        now = now or time.time()
        last = self._state["positions"].get(pos.symbol, {}).get("_last_settle", pos.opened_ts)
        while now - last >= 8 * 3600:
            rate = float(quote.get("fundingRate") or 0)
            # 多头付正费率, 空头收正费率(方向镜像)
            pay = pos.notional * rate * (1 if pos.direction == "long" else -1)
            pos.funding_paid += pay
            self._state["equity"] -= pay
            last += 8 * 3600
            log.info("[paper] %s 资金费率结算 %.6f (付费$%.4f)", pos.symbol, rate, pay)
        self._state["positions"][pos.symbol]["_last_settle"] = last

    def open(self, symbol: str, params: dict[str, Any], quote: dict[str, Any]) -> Position:
        qty = float(params["size"])
        direction = str(params.get("direction", "long"))
        # 多头以 ask 成交(买入), 空头以 bid 成交(卖出)
        if direction == "long":
            ask = float(quote.get("askPr") or 0) or float(quote.get("lastPr") or 0)
            fill = ask
        else:
            fill = float(quote.get("bidPr") or 0) or float(quote.get("lastPr") or 0)
        if fill <= 0:
            raise RuntimeError(f"{symbol} 无报价, 无法开仓")
        fee = qty * fill * self.cfg.paper.taker_fee
        self._state["equity"] -= fee
        pos = Position(symbol=symbol, qty=qty, avg_entry=fill, leverage=int(params["leverage"]),
                       sl=float(params["stop_loss"]), tp=float(params["take_profit"]),
                       opened_ts=time.time(),
                       volume_place=int(params.get("volume_place", 4) or 4),
                       direction=direction)
        pos.open_fee = fee
        self._state["positions"][symbol] = {**pos.to_dict(), "_last_settle": time.time()}
        self._save()
        log.info("[paper] 开仓 %s %s qty=%.4f @$%.2f 名义$%.2f 手续费$%.4f",
                 symbol, direction, qty, fill, qty * fill, fee)
        return pos

    def close(self, symbol: str, reason: str = "AI_CLOSE", price: float | None = None) -> dict[str, Any]:
        pmap = self._positions_map()
        pos = pmap.get(symbol)
        if not pos:
            return {"ok": False, "error": "no position"}
        q = self._quote_cache.get(symbol) or {}
        # 多头平仓以 bid 成交(卖出), 空头平仓以 ask 成交(买回)
        if pos.direction == "long":
            bid = price or float(q.get("bidPr") or 0) or float(q.get("lastPr") or 0)
            fill = float(bid)
        else:
            ask = price or float(q.get("askPr") or 0) or float(q.get("lastPr") or 0)
            fill = float(ask)
        if not fill:
            return {"ok": False, "error": "no quote"}
        fee = pos.qty * fill * self.cfg.paper.taker_fee
        open_fee = getattr(pos, "open_fee", 0.0) or 0.0
        if pos.direction == "long":
            pnl = (fill - pos.avg_entry) * pos.qty - fee - open_fee - pos.funding_paid
        else:
            pnl = (pos.avg_entry - fill) * pos.qty - fee - open_fee - pos.funding_paid
        self._state["equity"] += pnl + fee  # equity 已含 open_fee/funding 扣减, 只补毛利
        self._state["realized_pnl"] += pnl
        self._state["positions"].pop(symbol, None)
        self._state["closed"].append({
            "symbol": symbol, "qty": pos.qty, "entry": pos.avg_entry, "exit": fill,
            "pnl": pnl, "reason": reason, "ts": time.time(),
            "lev": pos.leverage, "sl": pos.sl, "tp": pos.tp, "direction": pos.direction,
        })
        self._save()
        log.info("[paper] 平仓 %s (%s) @$%.2f pnl $%+.4f (%s)",
                 symbol, pos.direction, fill, pnl, reason)
        return {"ok": True, "pnl": pnl, "exit": fill}

    def manage_tpsl(self, symbol: str, sl: float | None, tp: float | None) -> None:
        pos = self._positions_map().get(symbol)
        if pos:
            if sl:
                pos.sl = sl
            if tp:
                pos.tp = tp
            self._state["positions"][symbol] = {**pos.to_dict(),
                                                "_last_settle": self._state["positions"][symbol].get("_last_settle", time.time())}
            self._save()

    def _positions_map(self) -> dict[str, Position]:
        return {s: Position.from_dict({k: v for k, v in d.items() if k != "_last_settle"})
                for s, d in self._state["positions"].items()}

    def _positions(self) -> list[Position]:
        return list(self._positions_map().values())

    def closed_trades(self) -> list[dict[str, Any]]:
        return self._state.get("closed", [])


# ---------------------------------------------------------------------------
class RealExecutor:
    """真实执行(全仓): 先挂 TPSL 再开仓。实盘需 LIVE_CONFIRM=yes + 账户有余额。"""

    name = "real"

    def __init__(self, bg, cfg, state_dir: Path):
        self.bg = bg
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self._file = self.state_dir / "real.json"
        self._state = {"orders": []}
        try:
            if self._file.exists():
                self._state = json.loads(self._file.read_text())
        except Exception:
            pass

    def _save(self) -> None:
        _atomic_write(self._file, self._state)

    def account(self, symbol: str = "NVDAUSDT") -> dict[str, Any]:
        acc = self.bg.account(symbol)
        eq = float(acc.get("usdtEquity", 0) or 0)
        avail = float(acc.get("available", 0) or 0)
        pos = self.positions()
        notional = sum(p.notional for p in pos)
        longs = sum(1 for p in pos if p.direction == "long")
        shorts = sum(1 for p in pos if p.direction == "short")
        return {"equity": eq, "available": avail, "notional": notional,
                "position_count": len(pos), "long_count": longs, "short_count": shorts,
                "day_pnl": 0.0, "mode": "real"}

    def positions(self) -> list[Position]:
        out = []
        try:
            for r in self.bg.positions():
                if float(r.get("holdVol", 0) or 0) <= 0:
                    continue
                hold_side = r.get("holdSide", "long")
                if hold_side not in ("long", "short"):
                    continue
                out.append(Position(
                    symbol=r["symbol"],
                    qty=float(r["holdVol"]),
                    avg_entry=float(r["avgEntryPrice"]),
                    leverage=int(float(r.get("leverage", self.cfg.leverage) or self.cfg.leverage)),
                    opened_ts=float(r.get("openTimeAvg", 0) or 0) / 1000,
                    direction=hold_side,
                ))
        except Exception as e:
            log.error("获取持仓失败: %s", str(e)[:120])
        return out

    def _size_str(self, qty: float, volume_place: int = 4) -> str:
        """按合约 volumePlace 格式化数量(精度不符会被交易所拒单)。"""
        return f"{qty:.{max(1, volume_place)}f}"

    def open(self, symbol: str, params: dict[str, Any], quote: dict[str, Any]) -> Position:
        lev = int(params["leverage"])
        vp = int(params.get("volume_place", 4) or 4)
        direction = str(params.get("direction", "long"))
        side = "buy" if direction == "long" else "sell"
        hold_side = direction  # long/short 的仓位置
        # 1) 杠杆
        try:
            self.bg.set_leverage(symbol, lev, hold_side)
        except Exception as e:
            raise RuntimeError(f"set_leverage 失败: {str(e)[:80]}")
        # 2) 先挂 TPSL(方向对应 holdSide)
        tpsl_ids = []
        try:
            sl = self.bg.place_tpsl(symbol, "pos_loss", f"{float(params['stop_loss']):.4f}",
                                    hold_side=hold_side)
            tpsl_ids.append(sl.get("orderId", ""))
            tp = self.bg.place_tpsl(symbol, "pos_profit", f"{float(params['take_profit']):.4f}",
                                    hold_side=hold_side)
            tpsl_ids.append(tp.get("orderId", ""))
        except Exception as e:
            log.error("挂 TPSL 失败 %s: %s", symbol, str(e)[:120])
            raise RuntimeError(f"TPSL 挂单失败: {str(e)[:80]}")
        # 3) 市价开仓(方向对应 side/posSide)
        try:
            self.bg.place_order(symbol, side, self._size_str(float(params["size"]), vp),
                                order_type="market",
                                leverage=lev, margin_mode=self.cfg.margin_mode,
                                reduce_only=False)
        except Exception as e:
            # 回滚: 撤 TPSL
            for oid in tpsl_ids:
                try:
                    if oid:
                        self.bg.cancel_plan(symbol, oid)
                except Exception:
                    pass
            raise RuntimeError(f"开仓失败已回滚TPSL: {str(e)[:80]}")
        # 4) 回读核实
        time.sleep(2)
        pos = next((p for p in self.positions() if p.symbol == symbol), None)
        if pos is None:
            raise RuntimeError("开仓后回读不到持仓, 立即人工核对")
        self._state["orders"].append({
            "ts": time.time(), "symbol": symbol, "side": side, "qty": params["size"],
            "entry": pos.avg_entry, "sl": params["stop_loss"], "tp": params["take_profit"],
            "tpsl_ids": tpsl_ids, "direction": direction, "oid": pos.avg_entry,
        })
        self._save()
        log.info("[LIVE] 开仓 %s %s @$%.4f qty=%.4f", symbol, direction, pos.avg_entry, pos.qty)
        return pos

    def close(self, symbol: str, reason: str = "AI_CLOSE", price: float | None = None) -> dict[str, Any]:
        pos = next((p for p in self.positions() if p.symbol == symbol), None)
        if not pos:
            return {"ok": False, "error": "no position"}
        side = "sell" if pos.direction == "long" else "buy"  # 平仓方向镜像
        try:
            self.bg.place_order(symbol, side, self._size_str(pos.qty, pos.volume_place),
                                order_type="market",
                                leverage=pos.leverage, margin_mode=self.cfg.margin_mode,
                                reduce_only=True)
        except Exception as e:
            log.error("平仓失败 %s: %s", symbol, str(e)[:120])
            return {"ok": False, "error": str(e)[:80]}
        # 撤 TPSL 计划单
        try:
            for r in self.bg.pending_plans(symbol) or []:
                if r.get("planType") in ("pos_loss", "pos_profit"):
                    self.bg.cancel_plan(symbol, r["orderId"])
        except Exception as e:
            log.warning("撤 TPSL 计划单失败 %s: %s", symbol, str(e)[:80])
        time.sleep(2)
        gone = next((p for p in self.positions() if p.symbol == symbol), None)
        pnl = 0.0
        if gone is None:
            pnl = self._estimate_pnl_from_fills(pos)
        self._state["orders"].append({"ts": time.time(), "symbol": symbol, "side": "sell",
                                      "qty": pos.qty, "reason": reason, "pnl": pnl})
        self._save()
        log.info("[LIVE] 平仓 %s (%s) 估算pnl $%.4f", symbol, reason, pnl)
        return {"ok": True, "pnl": pnl, "exit": price or pos.avg_entry}

    def _estimate_pnl_from_fills(self, pos: Position) -> float:
        """从交易所成交记录(fills API)取真实已实现盈亏。权威对账。"""
        end = int(time.time() * 1000)
        start = int((time.time() - 3600) * 1000)
        try:
            d = self.bg._request(
                "GET",
                f"/api/v2/mix/order/fills?productType=USDT-FUTURES&symbol={pos.symbol}"
                f"&startTime={start}&endTime={end}",
            )
            fills = d.get("fillList", []) if isinstance(d, dict) else (d or [])
            pnl = sum(float(f.get("profit", 0) or 0) for f in fills
                      if f.get("tradeSide") == "close")
            return pnl
        except Exception as e:
            log.warning("fills 对账失败 %s: %s", pos.symbol, str(e)[:100])
            return 0.0

    def manage_tpsl(self, symbol: str, sl: float | None, tp: float | None) -> None:
        """差异>阈值才重挂: 撤旧 plan 再挂新。"""
        try:
            plan_map = {p.get("planType"): p for p in (self.bg.pending_plans(symbol) or [])}
        except Exception:
            plan_map = {}
        cur_sl, cur_tp = None, None
        if "pos_loss" in plan_map:
            cur_sl = float(plan_map["pos_loss"].get("triggerPrice", 0))
        if "pos_profit" in plan_map:
            cur_tp = float(plan_map["pos_profit"].get("triggerPrice", 0))
        quote_last = 0.0
        try:
            quote_last = float(self.bg.quote(symbol).get("lastPr", 0))
        except Exception:
            pass
        diff = self.cfg.stop_repost_diff_pct / 100.0
        if sl and cur_sl and quote_last and abs(sl - cur_sl) / quote_last <= diff:
            sl = None  # 差异小, 不重挂
        if tp and cur_tp and quote_last and abs(tp - cur_tp) / quote_last <= diff:
            tp = None
        if not sl and not tp:
            return
        for plan_type, new_v in (("pos_loss", sl), ("pos_profit", tp)):
            if new_v is None:
                continue
            oid = (plan_map.get(plan_type) or {}).get("orderId", "")
            try:
                if oid:
                    self.bg.cancel_plan(symbol, oid)
                self.bg.place_tpsl(symbol, plan_type, f"{new_v:.4f}")
                log.info("[LIVE] 更新 %s %s → %.4f", symbol, plan_type, new_v)
            except Exception as e:
                log.error("更新 TPSL 失败 %s/%s: %s", symbol, plan_type, str(e)[:100])

    def closed_trades(self) -> list[dict[str, Any]]:
        return self._state.get("orders", [])