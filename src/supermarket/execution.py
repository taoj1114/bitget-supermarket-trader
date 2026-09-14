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
    batches: int = 1          # 分批建仓批次(超市补货, 上限3)

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
                                                                    "volume_place", "direction",
                                                                    "batches")})


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
        # 分批建仓(超市补货): 同标的同方向 → 合并批次, 加权均价
        existing = self._state["positions"].get(symbol)
        if existing and existing.get("direction", "long") == direction:
            old_qty = float(existing["qty"]); old_entry = float(existing["avg_entry"])
            new_qty = old_qty + qty
            existing["qty"] = new_qty
            existing["avg_entry"] = (old_entry * old_qty + fill * qty) / new_qty
            existing["open_fee"] = float(existing.get("open_fee", 0)) + fee
            # 止损只收紧不放宽(保护不被新批削弱): 多头取更高者, 空头取更低者
            old_sl = float(existing.get("sl", 0) or 0)
            new_sl = float(params["stop_loss"])
            if old_sl > 0 and new_sl > 0:
                keep_sl = max(old_sl, new_sl) if direction == "long" else min(old_sl, new_sl)
            else:
                keep_sl = new_sl or old_sl
            existing["sl"] = keep_sl
            existing["tp"] = float(params["take_profit"])
            existing["leverage"] = int(params["leverage"])
            existing["batches"] = int(existing.get("batches", 1)) + 1
            self._save()
            b = existing["batches"]
            log.info("[paper] 加仓 %s %s 批次%d qty=%.4f @$%.2f 合并后均价$%.2f 名义$%.2f 手续费$%.4f",
                     symbol, direction, b, qty, fill, existing["avg_entry"], new_qty * existing["avg_entry"], fee)
            return Position(symbol=symbol, qty=new_qty, avg_entry=existing["avg_entry"],
                            leverage=int(params["leverage"]), sl=existing["sl"], tp=existing["tp"],
                            opened_ts=float(existing["opened_ts"]), volume_place=int(existing["volume_place"]),
                            direction=direction, batches=b, open_fee=existing["open_fee"])
        pos = Position(symbol=symbol, qty=qty, avg_entry=fill, leverage=int(params["leverage"]),
                       sl=float(params["stop_loss"]), tp=float(params["take_profit"]),
                       opened_ts=time.time(),
                       volume_place=int(params.get("volume_place", 4) or 4),
                       direction=direction, batches=1)
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
        funding_paid = getattr(pos, "funding_paid", 0.0) or 0.0
        if pos.direction == "long":
            pnl = (fill - pos.avg_entry) * pos.qty - fee - open_fee - funding_paid
        else:
            pnl = (pos.avg_entry - fill) * pos.qty - fee - open_fee - funding_paid
        # 账目闭环: pnl 已扣全部费用(平仓费+开仓费+资金费); 开仓费/资金费已在
        # 开仓与结算时从 equity 扣过, 故此处只补「毛利-平仓费」= pnl + open_fee + funding_paid
        self._state["equity"] += pnl + open_fee + funding_paid
        self._state["realized_pnl"] = self._state.get("realized_pnl", 0.0) + pnl
        self._state["positions"].pop(symbol, None)
        self._state["closed"].append({
            "symbol": symbol, "qty": pos.qty, "entry": pos.avg_entry, "exit": fill,
            "pnl": pnl, "reason": reason, "ts": time.time(),
            "lev": pos.leverage, "sl": pos.sl, "tp": pos.tp, "direction": pos.direction,
            "close_fee": fee, "open_fee": open_fee, "funding_paid": funding_paid,
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
        self._state: dict[str, Any] = {"orders": [], "holdings": {}}
        self._quote_cache: dict[str, dict] = {}
        self._contracts: dict[str, dict] = {}
        try:
            if self._file.exists():
                d = json.loads(self._file.read_text())
                self._state = {"orders": d.get("orders", []), "holdings": d.get("holdings", {})}
        except Exception:
            pass

    def set_quote(self, symbol: str, quote: dict[str, Any]) -> None:
        """接口兼容(Paper 同名): 缓存最新报价供 _holding_line/AI 使用。"""
        self._quote_cache[symbol] = quote

    def tick(self) -> None:
        """实盘无需本地 tick: 止盈止损由交易所侧 TPSL 实时执行。"""
        return

    def _vp(self, symbol: str) -> int:
        """合约数量精度(volumePlace); 缓存一次。"""
        if not self._contracts:
            try:
                self._contracts = {c["symbol"]: c for c in self.bg.stock_contracts()}
            except Exception as e:
                log.warning("合约表加载失败: %s", str(e)[:80])
        return int((self._contracts.get(symbol) or {}).get("volumePlace", 4) or 4)

    def _hold(self, symbol: str) -> dict[str, Any]:
        return self._state.setdefault("holdings", {}).get(symbol, {})

    def _save(self) -> None:
        _atomic_write(self._file, self._state)

    def account(self, symbol: str = "NVDAUSDT") -> dict[str, Any]:
        a = self.bg.v3_account()
        pos = self.positions()
        notional = sum(p.notional for p in pos)
        longs = sum(1 for p in pos if p.direction == "long")
        shorts = sum(1 for p in pos if p.direction == "short")
        return {"equity": a["equity"], "available": a["available"], "notional": notional,
                "position_count": len(pos), "long_count": longs, "short_count": shorts,
                "day_pnl": 0.0, "mode": "real"}

    def positions(self) -> list[Position]:
        out = []
        try:
            for r in self.bg.v3_positions():
                sym = r["symbol"]
                h = self._hold(sym)
                qp, _ = self.bg.v3_precision(sym)
                out.append(Position(
                    symbol=sym,
                    qty=float(r.get("total", 0) or 0),
                    avg_entry=float(r.get("avgPrice", 0) or 0),
                    leverage=int(float(r.get("leverage", self.cfg.leverage) or self.cfg.leverage)),
                    opened_ts=float(r.get("createdTime", 0) or 0) / 1000,
                    direction=str(r.get("posSide")),
                    volume_place=qp,
                    sl=float(h.get("sl", 0) or 0),
                    tp=float(h.get("tp", 0) or 0),
                    batches=int(h.get("batches", 1) or 1),
                ))
        except Exception as e:
            log.error("获取持仓失败: %s", str(e)[:120])
        return out

    def _size_str(self, qty: float, volume_place: int = 4) -> str:
        """按合约 volumePlace 格式化数量(精度不符会被交易所拒单)。"""
        return f"{qty:.{max(1, volume_place)}f}"

    def open(self, symbol: str, params: dict[str, Any], quote: dict[str, Any]) -> Position:
        lev = int(params["leverage"])
        direction = str(params.get("direction", "long"))
        side = "buy" if direction == "long" else "sell"
        qty = float(params["size"])
        sl = float(params["stop_loss"])
        tp = float(params["take_profit"])
        # 1) 杠杆(v3)
        try:
            self.bg.v3_set_leverage(symbol, lev, self.cfg.margin_mode)
        except Exception as e:
            raise RuntimeError(f"set_leverage 失败: {str(e)[:80]}")
        # 2) 市价开仓 + preset TP/SL(v3 一步到位: 交易所侧 TPSL, 无裸奔窗口)
        oid = ""
        try:
            res = self.bg.v3_place_order(symbol, side, qty, pos_side=direction,
                                         order_type="market", stop_loss=sl, take_profit=tp,
                                         client_oid=f"hopen{int(time.time()*1000)}"[:32])
            oid = (res or {}).get("orderId", "")
        except Exception as e:
            raise RuntimeError(f"开仓失败: {str(e)[:100]}")
        # 3) 回读持仓 + TPSL 策略单确认
        time.sleep(2)
        pos = next((p for p in self.positions()
                    if p.symbol == symbol and p.direction == direction), None)
        if pos is None:
            raise RuntimeError(f"开仓后回读不到持仓(订单{oid}), 立即人工核对")
        tpsl_ids: list[str] = []
        try:
            for s in self.bg.v3_strategy_orders(symbol):
                if str(s.get("posSide")) == direction:
                    tpsl_ids.append(str(s.get("orderId", "")))
            if not tpsl_ids:
                log.error("⚠️ %s 开仓后未发现 TPSL 策略单(订单%s), 请人工核查保护!", symbol, oid)
        except Exception as e:
            log.warning("TPSL 策略单查询失败 %s: %s", symbol, str(e)[:80])
        # 4) 本地记录(SL 只收紧不放宽)
        h = self._state.setdefault("holdings", {}).get(symbol) or {}
        old_sl = float(h.get("sl", 0) or 0)
        if old_sl > 0 and sl > 0:
            keep_sl = max(old_sl, sl) if direction == "long" else min(old_sl, sl)
        else:
            keep_sl = sl or old_sl
        self._state["holdings"][symbol] = {
            "sl": keep_sl, "tp": tp, "batches": int(h.get("batches", 0)) + 1,
            "direction": direction, "opened_ts": h.get("opened_ts") or time.time(),
            "tpsl_ids": tpsl_ids,
        }
        self._state["orders"].append({
            "ts": time.time(), "symbol": symbol, "side": side, "qty": qty,
            "entry": pos.avg_entry, "sl": sl, "tp": tp, "order_id": oid,
            "tpsl_ids": tpsl_ids, "direction": direction, "action": "open",
        })
        self._save()
        log.info("[LIVE] 开仓 %s %s @$%.4f qty=%.4f SL=%s TP=%s 订单%s TPSL%s",
                 symbol, direction, pos.avg_entry, pos.qty, sl, tp, oid, tpsl_ids)
        try:
            from supermarket import notify
            notify.trade_open(symbol, direction, pos.avg_entry, pos.qty, sl, tp,
                              batches=int(self._state["holdings"][symbol].get("batches", 1)))
        except Exception:
            pass
        return pos

    def close(self, symbol: str, reason: str = "AI_CLOSE", price: float | None = None) -> dict[str, Any]:
        pos = next((p for p in self.positions() if p.symbol == symbol), None)
        if not pos:
            return {"ok": False, "error": "no position"}
        # 平仓(hedge: 只传 posSide, ⚠️不能带 reduceOnly → 25238) — 失败重试3次
        last_err = ""
        for attempt in range(1, 4):
            try:
                self.bg.v3_close_order(symbol, pos.qty, pos.direction)
                last_err = ""
                break
            except Exception as e:
                last_err = str(e)[:120]
                log.warning("平仓失败(第%d次) %s: %s", attempt, symbol, last_err)
                if attempt < 3:
                    time.sleep(2.5)
        if last_err:
            log.error("❌ 平仓3次均失败 %s: %s → 下轮重试 + 告警", symbol, last_err)
            self._alert("CLOSE_FAIL", symbol, last_err)
            return {"ok": False, "error": last_err[:80]}
        # 清理残余 TPSL 策略单(平仓后一般自动撤, 保险起见)
        try:
            for s in self.bg.v3_strategy_orders(symbol):
                if str(s.get("posSide")) == pos.direction:
                    self.bg.v3_cancel_strategy(symbol, str(s.get("orderId", "")))
        except Exception as e:
            log.warning("撤策略单失败 %s: %s", symbol, str(e)[:80])
        time.sleep(2)
        gone = next((p for p in self.positions()
                     if p.symbol == symbol and p.direction == pos.direction), None)
        pnl = 0.0
        if gone is None:
            pnl = self.bg.v3_last_closed_pnl(symbol)   # fills 真实已实现盈亏(权威)
        self._state.setdefault("holdings", {}).pop(symbol, None)
        self._state["orders"].append({"ts": time.time(), "symbol": symbol,
                                      "side": "sell" if pos.direction == "long" else "buy",
                                      "qty": pos.qty, "action": "close",
                                      "reason": reason, "pnl": pnl,
                                      "direction": pos.direction})
        self._save()
        log.info("[LIVE] 平仓 %s (%s) 真实pnl $%.4f", symbol, reason, pnl)
        try:
            from supermarket import notify
            notify.trade_close(symbol, pos.direction, pnl, reason)
        except Exception:
            pass
        return {"ok": True, "pnl": pnl, "exit": price or pos.avg_entry}

    def closed_pnl(self, symbol: str) -> float:
        """该 symbol 最近一笔平仓的真实已实现盈亏(v3 fills, 实盘对账权威)。"""
        return self.bg.v3_last_closed_pnl(symbol)

    # ---------- 保护自愈 / 告警(2026-09 实盘健壮性) ----------

    def _alert(self, kind: str, symbol: str, detail: str) -> None:
        """关键异常: 落盘告警 + Telegram 推送(裸仓/平仓失败等)。"""
        try:
            alerts = self._state.setdefault("alerts", [])
            alerts.append({"ts": time.time(), "kind": kind, "symbol": symbol,
                           "detail": detail[:200]})
            del alerts[:-50]           # 只留最近50条
            self._save()
        except Exception:
            pass
        try:
            from supermarket import notify
            notify.alert(kind, symbol, detail)
        except Exception:
            pass

    def unprotected_positions(self) -> list[Position]:
        """持仓中没有 TPSL 策略单保护的(裸仓)。"""
        out = []
        try:
            strat = {}
            for s in self.bg.v3_strategy_orders():
                strat.setdefault((s.get("symbol"), s.get("posSide")), s)
            for p in self.positions():
                if (p.symbol, p.direction) not in strat:
                    out.append(p)
        except Exception as e:
            log.warning("裸仓检测查询失败: %s", str(e)[:100])
        return out

    def repair_protection(self, pos: Position, replace: bool = False) -> bool:
        """给裸仓(或仓量不匹配的仓)补挂 TPSL; 优先本地记录, 否则按均价保守兜底 -2.5%/+4%。"""
        h = self._hold(pos.symbol)
        sl = float(h.get("sl", 0) or 0)
        tp = float(h.get("tp", 0) or 0)
        if not sl or (pos.direction == "long" and sl >= pos.avg_entry) or \
           (pos.direction == "short" and sl <= pos.avg_entry):
            sl = pos.avg_entry * (0.975 if pos.direction == "long" else 1.025)
        if not tp:
            tp = pos.avg_entry * (1.04 if pos.direction == "long" else 0.96)
        if replace:
            try:
                for s in self.bg.v3_strategy_orders(pos.symbol):
                    if str(s.get("posSide")) == pos.direction:
                        self.bg.v3_cancel_strategy(pos.symbol, str(s.get("orderId", "")))
            except Exception as e:
                log.warning("撤旧策略单失败 %s: %s", pos.symbol, str(e)[:80])
        qp, pp = self.bg.v3_precision(pos.symbol)
        try:
            res = self.bg._request("POST", "/api/v3/trade/place-strategy-order", {
                "category": "USDT-FUTURES", "symbol": pos.symbol,
                "type": "tpsl", "qty": f"{pos.qty:.{qp}f}", "posSide": pos.direction,
                "stopLoss": f"{sl:.{pp}f}", "slTriggerBy": "mark",
                "takeProfit": f"{tp:.{pp}f}", "tpTriggerBy": "mark",
            })
            self._alert("PROTECTION_REPAIRED", pos.symbol,
                        f"qty={pos.qty} SL={sl:.2f} TP={tp:.2f} replace={replace} {res}")
            log.warning("🔧 保护补挂 %s %s qty=%s SL=%.2f TP=%.2f", pos.symbol, pos.direction, pos.qty, sl, tp)
            hh = self._state.setdefault("holdings", {}).setdefault(pos.symbol, {})
            hh.update({"sl": sl, "tp": tp, "direction": pos.direction})
            self._save()
            return True
        except Exception as e:
            log.error("❌ 保护补挂失败 %s: %s", pos.symbol, str(e)[:120])
            self._alert("PROTECTION_REPAIR_FAIL", pos.symbol, str(e)[:150])
            return False

    def manage_tpsl(self, symbol: str, sl: float | None, tp: float | None) -> None:
        """修改持仓 TPSL 策略单(差异>阈值才动)。"""
        pos = next((p for p in self.positions() if p.symbol == symbol), None)
        if pos is None:
            log.warning("manage_tpsl 无持仓 %s, 跳过", symbol)
            return
        try:
            stra = [s for s in self.bg.v3_strategy_orders(symbol)
                    if str(s.get("posSide")) == pos.direction]
        except Exception as e:
            log.warning("查策略单失败 %s: %s", symbol, str(e)[:80])
            return
        if not stra:
            log.warning("manage_tpsl %s: 无 TPSL 策略单可改(跳过)", symbol)
            return
        oid = str(stra[0].get("orderId", ""))
        cur_sl = float(stra[0].get("stopLoss") or 0)
        cur_tp = float(stra[0].get("takeProfit") or 0)
        quote_last = 0.0
        try:
            quote_last = float(self.bg.quote(symbol).get("lastPr", 0) or 0)
        except Exception:
            pass
        diff = self.cfg.stop_repost_diff_pct / 100.0
        if sl and cur_sl and quote_last and abs(sl - cur_sl) / quote_last <= diff:
            sl = None
        if tp and cur_tp and quote_last and abs(tp - cur_tp) / quote_last <= diff:
            tp = None
        if not sl and not tp:
            return
        # 止损只收紧不放宽(多头不上移放宽, 空头不下移放宽)
        if sl and cur_sl:
            if pos.direction == "long" and sl < cur_sl:
                sl = None
            elif pos.direction == "short" and sl > cur_sl:
                sl = None
        if not sl and not tp:
            return
        try:
            self.bg.v3_modify_strategy(symbol, oid, stop_loss=sl, take_profit=tp)
            log.info("[LIVE] 更新 TPSL %s %s → SL=%s TP=%s", symbol, pos.direction, sl, tp)
            h = self._state.setdefault("holdings", {}).get(symbol)
            if h:
                if sl:
                    h["sl"] = float(sl)
                if tp:
                    h["tp"] = float(tp)
                self._save()
        except Exception as e:
            log.error("更新 TPSL 失败 %s: %s", symbol, str(e)[:100])

    def closed_trades(self) -> list[dict[str, Any]]:
        return self._state.get("orders", [])