"""主引擎: 账户 → 对账 → 管仓 → 扫描开仓 → 复盘, 30分钟一轮(中线, 周末停机)。

run_once() 顺序(见 docs/PSEUDOCODE.md):
0. 账户快照 + 日基线 + 熔断
1. 对账(交易所/纸面成交 vs 本地记忆, 补录被 SL/TP 平掉的仓)
2. 管仓(每 tick 必跑, 所有持仓)
3. 扫描开仓(受风控约束的标的轮转)
4. 复盘触发器
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from supermarket import __version__
from supermarket.ai import build_provider
from supermarket.bitget_client import BitgetClient
from supermarket.config import Config, load_dotenv
from supermarket.execution import PaperExecutor, RealExecutor
from supermarket.market import MarketData
from supermarket.memory import AIMemory
from supermarket.prompts import (
    SYSTEM_MANAGE,
    SYSTEM_OPEN,
    build_manage_prompt,
    build_open_prompt,
)
from supermarket.risk import RiskEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engine")


class SupermarketEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        load_dotenv(".env")
        base_state = cfg.state_path
        # 模式状态隔离(2026-09 实盘教训): 实盘/纸面各自独立的 memory/风控/持仓文件,
        # 否则 paper 历史决策会在实盘启动时被误对账 → 乱补录 + 假熔断
        if cfg.mode == "real":
            self.state_dir = base_state / "live"
            self.state_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.state_dir = base_state
        self.bg = BitgetClient(cfg.bitget.base_url, cfg.bitget.api_key,
                               cfg.bitget.secret, cfg.bitget.passphrase)
        self.market = MarketData(self.bg, cfg=self.cfg)
        self.provider = build_provider(cfg)
        self.risk = RiskEngine(cfg, self.state_dir)
        self.memory = AIMemory(self.state_dir)
        if cfg.mode == "real":
            self.executor: Any = RealExecutor(self.bg, cfg, self.state_dir)
        else:
            self.executor = PaperExecutor(cfg, self.state_dir)
        self._contracts: dict[str, dict] = {}
        self._contracts_ts = 0.0
        self._scan_rotate = 0
        self._last_daily: dict[str, float] = {}

    # ---------- 合约池 ----------
    def _load_contracts(self) -> None:
        """美股合约缓存(5分钟TTL; 每轮全量拉取787合约是浪费)。"""
        now = time.time()
        if now - self._contracts_ts < 300:
            return
        try:
            for c in self.bg.stock_contracts():
                self._contracts[c["symbol"]] = c
            self._contracts_ts = now
            log.info("美股合约缓存 %d 个", len(self._contracts))
        except Exception as e:
            log.warning("合约列表获取失败: %s", str(e)[:80])

    def _contract(self, symbol: str) -> dict:
        if symbol not in self._contracts:
            try:
                for c in self.bg.contracts():
                    self._contracts[c["symbol"]] = c
            except Exception:
                pass
        return self._contracts.get(symbol, {"maxLever": 20, "sizeMultiplier": 0.01,
                                            "minTradeUSDT": 5})

    # ---------- 账户快照 ----------
    def _account(self) -> dict[str, Any]:
        try:
            acc = self.executor.account()
            # 今日已实现盈亏由风控引擎维护, 注入给 AI 看(此前恒为0.00的bug)
            try:
                acc["day_pnl"] = round(float(self.risk.state.day_pnl), 4)
            except Exception:
                acc.setdefault("day_pnl", 0.0)
            return acc
        except Exception as e:
            log.error("账户快照失败: %s", str(e)[:100])
            return {"equity": 0, "available": 0, "notional": 0, "position_count": 0,
                    "day_pnl": 0, "mode": self.executor.name}

    # ---------- 1. 对账 ----------
    def _reconcile(self) -> None:
        """本地 open decisions 与实际持仓比对; 交易所/纸面已平的仓 → 补录。"""
        held = {p.symbol for p in self.executor.positions()}
        # paper 模式优先用执行器记录的真实已平仓(含准确 pnl/exit)
        paper_closed: dict[str, dict] = {}
        if self.executor.name == "paper":
            for c in self.executor.closed_trades():
                paper_closed.setdefault(c["symbol"], c)  # 保留最近一条
        for d in self.memory.open_decisions():
            sym = d["symbol"]
            if sym in held:
                continue
            # 已被外部平掉(交易所 SL/TP 或纸面穿价)
            pc = paper_closed.get(sym)
            if pc:
                price = float(pc.get("exit", 0))
                pnl = float(pc.get("pnl", 0.0))
            else:
                price = 0.0
                try:
                    price = float(self.bg.quote(sym).get("lastPr", 0))
                except Exception:
                    pass
                pnl = 0.0
                # 实盘优先: fills API 的真实已实现盈亏(权威对账)
                if hasattr(self.executor, "closed_pnl"):
                    pnl = float(self.executor.closed_pnl(sym))
                if pnl == 0.0:
                    entry = float(d.get("entry", 0))
                    if entry > 0 and price > 0:
                        pnl = (price - entry) / entry * float((d.get("params") or {}).get("notional", 0))
            self.memory.close_decision(sym, price, pnl, "EXCHANGE_SLTP(对账补录)",
                                       max_pnl_pct=float(d.get("max_pnl_pct", 0)))
            self.risk.on_close(pnl)
            log.info("对账: %s 已被外部平仓, 补录 pnl $%.4f", sym, pnl)
            try:
                from supermarket import notify
                notify.trade_close(sym, str(d.get("action", "")).lower() or "long",
                                   pnl, "交易所侧TPSL触发(对账补录)")
            except Exception:
                pass

    def _ensure_protection(self) -> None:
        """保护自愈(实盘安全网): 裸仓补挂 TPSL + 策略单量不匹配时重挂覆盖全仓。

        实盘首日的健壮性缺口: 开仓时 TPSL 若挂失败/被异常撤销/加仓后量不匹配,
        持仓就会失去交易所侧保护 —— 这里每轮自动检测并修复。
        """
        if not hasattr(self.executor, "unprotected_positions"):
            return  # paper 模式无需(本地 TPSL 模拟)
        try:
            for p in self.executor.unprotected_positions():
                log.error("⚠️ 裸仓(无 TPSL 保护) %s %s qty=%s → 自动补挂", p.symbol, p.direction, p.qty)
                self.executor.repair_protection(p)
            try:
                strat = {(s.get("symbol"), str(s.get("posSide"))): s
                         for s in self.bg.v3_strategy_orders()}
            except Exception:
                strat = {}
            for p in self.executor.positions():
                s = strat.get((p.symbol, p.direction))
                if not s:
                    continue
                try:
                    sq = float(s.get("qty", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if sq and abs(sq - p.qty) > max(0.01, p.qty * 0.02):
                    log.warning("策略单量(%s)≠持仓(%s) %s → 重挂覆盖全仓", sq, p.qty, p.symbol)
                    self.executor.repair_protection(p, replace=True)
        except Exception as e:
            log.warning("保护自愈异常: %s", str(e)[:120])

    # ---------- 2. 管仓 ----------
    def _manage_positions(self, account: dict) -> None:
        positions = self.executor.positions()
        if not positions:
            return
        lessons = self.memory.lessons

        def probe(pos) -> tuple | None:
            """并行阶段: 取行情 + 构建输入 + AI 决策(只读)。"""
            sym = pos.symbol
            entry = pos.avg_entry
            try:
                quote = self.bg.quote(sym)
                if not quote:
                    return None
                last = float(quote.get("lastPr", 0) or 0)
                if last <= 0:
                    return None
                # 浮盈亏按方向计算(空单价格下跌=盈利)
                if pos.direction == "short":
                    pnl_pct = (entry - last) / entry * 100 if entry else 0.0
                else:
                    pnl_pct = (last - entry) / entry * 100 if entry else 0.0
                inp = self.market.build_input(sym, quote, account,
                                              lessons=lessons, manage=True)
                decision = self.provider.decide_manage(
                    SYSTEM_MANAGE, build_manage_prompt(inp, {
                        "symbol": sym, "avg_entry": entry, "notional": pos.notional,
                        "unrealized_pnl_pct": pnl_pct, "direction": pos.direction,
                        "hold_days": round((time.time() - pos.opened_ts) / 86400, 1),
                        "batches": getattr(pos, "batches", 1),
                    }))
                return (pos, quote, decision)
            except Exception as e:
                log.error("管仓 %s 决策异常: %s", sym, str(e)[:80])
                return None

        results = []
        if len(positions) > 1:
            with ThreadPoolExecutor(max_workers=min(self.cfg.scan_workers, len(positions))) as ex:
                results = [r for r in ex.map(probe, positions) if r]
        else:
            r = probe(positions[0])
            results = [r] if r else []

        # 串行执行阶段(状态写)
        for pos, quote, decision in results:
            sym = pos.symbol
            self.executor.set_quote(sym, quote)
            last = float(quote.get("lastPr", 0) or 0)
            entry = pos.avg_entry
            if entry > 0:
                # 浮盈按方向: 用于 T+N 评估(max_pnl 永远是盈利方向)
                if pos.direction == "short":
                    self.memory.set_max_pnl(sym, (entry - last) / entry * 100)
                else:
                    self.memory.set_max_pnl(sym, (last - entry) / entry * 100)
            log.info("管仓 %s → %s | %s", sym, decision.action, decision.reason[:60])
            if decision.is_close:
                res = self.executor.close(sym, reason="AI_CLOSE")
                if res.get("ok"):
                    pnl = float(res.get("pnl", 0))
                    self.memory.close_decision(sym, float(res.get("exit", last)), pnl,
                                               "AI_CLOSE", max_pnl_pct=0)
                    self.risk.on_close(pnl)
                else:
                    log.error("管仓平仓失败 %s: %s", sym, res.get("error"))
            elif decision.action == "ADJUST":
                self.executor.manage_tpsl(sym, decision.stop_loss, decision.take_profit)
            # HOLD: 不动

    def _holding_line(self, sym: str) -> str:
        """已持仓信息(批次/均价/浮盈/SL/TP), 给开仓AI决定加仓。"""
        for p in self.executor.positions():
            if p.symbol == sym:
                q = self.bg.quote(sym)
                last = float(q.get("lastPr", 0) or 0)
                if p.direction == "long":
                    upnl = (last - p.avg_entry) / p.avg_entry * 100
                else:
                    upnl = (p.avg_entry - last) / p.avg_entry * 100
                return (f"已持仓第{p.batches}批(上限{self.cfg.max_batches_per_symbol}) "
                        f"qty{p.qty:.1f} 均价${p.avg_entry:.2f} 浮盈{upnl:+.2f}% SL={p.sl} TP={p.tp}")
        return ""

    # ---------- 3. 扫描开仓 ----------
    def _pick_candidates(self, account: dict) -> list[str]:
        """候选构成: 持仓(补货/管理) + 热门前2 + 异动前3 + 轮转补齐(按成交额排序, 指针推进)。

        2026-09 修复: 原流动性门槛$5M+hot前3导致候选枯竭(池211只实际只扫6只, 轮转永不推进)。
        """
        held = [p.symbol for p in self.executor.positions()]
        try:
            tickers = self.market.tickers()
        except Exception as e:
            log.error("ticker 获取失败: %s", str(e)[:80])
            return []
        vol = {t["symbol"]: float(t.get("usdtVolume", 0) or 0) for t in tickers}
        # 异动度(24h涨跌绝对值): 超市关注热销/异动货源
        chg = {}
        for t in tickers:
            try:
                chg[t["symbol"]] = abs(float(t.get("changeUtc24h") or 0))
            except (TypeError, ValueError):
                chg[t["symbol"]] = 0.0
        # 流动性门槛(ceiling: 保留全部经 hot 白名单)
        valid = [s for s in vol
                 if s in self._contracts and s not in held
                 and (vol[s] >= self.cfg.min_turnover_floor or s in set(self.cfg.hot_symbols))]
        hot = [s for s in self.cfg.hot_symbols if s in valid]
        movers = sorted((s for s in valid if s not in hot), key=lambda s: -chg.get(s, 0))[:3]
        rest = sorted((s for s in valid if s not in hot and s not in movers), key=lambda s: -vol[s])
        # 已持仓标的一起入轮(看得见才能决定加仓/减仓/补货), 但不超过6个
        out = list(held[:6]) + hot[:2] + movers
        n = self.cfg.max_symbols_per_round - len(out)
        if n > 0 and rest:
            rotated = rest[self._scan_rotate:] + rest[:self._scan_rotate]
            picked = rotated[:n]
            out += picked
            self._scan_rotate = (self._scan_rotate + len(picked)) % max(len(rest), 1)
            log.info("候选轮转: 池%d只(持仓%d/热门%d/异动%d/轮转%d, 指针→%d)",
                     len(valid) + len(held), len(out[:len(held)]), 2, len(movers),
                     len(picked), self._scan_rotate)
        return out

    def _scan(self, account: dict) -> None:
        if account.get("equity", 0) <= 0:
            log.warning("净值0, 跳过扫描")
            return
        paused = self.risk.paused()
        if paused:
            log.info("熔断: %s", paused)
            if not getattr(self, "_was_paused", False):
                try:
                    from supermarket import notify
                    notify.risk_event(f"熔断触发: {paused}")
                except Exception:
                    pass
            self._was_paused = True
            return
        self._was_paused = False
        # 人工暂停开关(/pause 指令写入 pause.flag)
        if (self.state_dir / "pause.flag").exists():
            log.info("已暂停开新仓(/resume 恢复), 跳过扫描")
            return
        # 天气门(硬约束): SPY 24h 跌>3% = 系统性雨天, 禁开新仓(已有库存照常管)
        try:
            spyq = self.bg.quote("SPYUSDT")
            spy_chg = float(spyq.get("changeUtc24h") or 0) * 100
            if spy_chg <= self.cfg.spy_drop_gate_pct:
                log.warning("天气门: SPY 24h %.2f%% ≤ %.1f%%, 当日禁开新仓",
                            spy_chg, self.cfg.spy_drop_gate_pct)
                try:
                    from supermarket import notify
                    notify.risk_event(f"天气门触发: SPY 24h {spy_chg:+.2f}% ≤ "
                                      f"{self.cfg.spy_drop_gate_pct}% → 当日禁开新仓")
                except Exception:
                    pass
                return
        except Exception:
            pass  # 拿不到 SPY 不阻塞(如网络抖动)
        # 大盘趋势门控(硬约束, 2026-09 用户实证): SPY 日线明确向下 → 禁开多仓
        # 理由: 天气门只挡单日暴跌, 挡不住"阴跌"里反复抄反弹→认错小亏的累积(实测两笔 -$0.79)
        self._market_down = False
        self._market_note = ""
        try:
            mr = self.market.daily_regime("SPYUSDT")
            if mr:
                reg, madx = mr
                self._market_note = f"SPY日线{reg}(ADX{madx:.0f})"
                if reg == "trend_down" and madx >= self.cfg.market_down_adx:
                    self._market_down = True
                    log.warning("大盘趋势门控: %s → 禁开多仓(空单/深跌反转例外)", self._market_note)
                    if not getattr(self, "_market_down_notified", False):
                        self._market_down_notified = True
                        try:
                            from supermarket import notify
                            notify.risk_event(f"大盘趋势门控: {self._market_note} → 禁开多仓"
                                              f"(空单/深跌反转例外仍允许)")
                        except Exception:
                            pass
                else:
                    self._market_down_notified = False
        except Exception as e:
            log.debug("大盘趋势门控检测失败: %s", str(e)[:60])

        # 市场情绪评分(用户提议 2026-09): 池内宽度 + VIX → 事实输入给 AI(不做程序决策)
        self._sentiment = {}
        self._sentiment_line = ""
        try:
            from supermarket.sentiment import compute_sentiment, fetch_vix, format_line
            ts_map = {t["symbol"]: t for t in self.market.tickers()}
            chgs = []
            for s in self._contracts:
                tk = ts_map.get(s)
                if not tk:
                    continue
                try:
                    chgs.append(float(tk.get("changeUtc24h") or 0) * 100)
                except (TypeError, ValueError):
                    pass
            self._sentiment = compute_sentiment(chgs, fetch_vix())
            self._sentiment_line = format_line(self._sentiment)
            if self._sentiment_line:
                log.info("情绪: %s", self._sentiment_line)
        except Exception as e:
            log.debug("情绪评分失败(不阻塞): %s", str(e)[:60])

        candidates = self._pick_candidates(account)
        lessons = self.memory.lessons
        session = __import__("supermarket.market", fromlist=["us_session"]).us_session()

        def probe(sym) -> tuple | None:
            """并行阶段: 取行情 + 构建 AIInput + AI 决策(只读)。"""
            try:
                quote = self.bg.quote(sym)
                if not quote or float(quote.get("lastPr", 0) or 0) <= 0:
                    return None
                history = self.memory.get_symbol_history(sym)
                inp = self.market.build_input(sym, quote, account,
                                              history=history, lessons=lessons,
                                              current_holding=self._holding_line(sym),
                                              extra_env=getattr(self, "_sentiment_line", ""))
                decision = self.provider.decide_open(SYSTEM_OPEN, build_open_prompt(inp))
                return (sym, quote, inp, decision)
            except Exception as e:
                log.error("扫描 %s 异常: %s", sym, str(e)[:80])
                return None

        results = []
        if len(candidates) > 1:
            with ThreadPoolExecutor(max_workers=min(self.cfg.scan_workers, len(candidates))) as ex:
                results = [r for r in ex.map(probe, candidates) if r]
        else:
            r = probe(candidates[0]) if candidates else None
            results = [r] if r else []

        # 串行执行阶段(风控校验+开仓, 防并发超仓)
        for sym, quote, inp, decision in results:
            self.executor.set_quote(sym, quote)
            if decision.is_buy or decision.is_short:
                side = "long" if decision.is_buy else "short"
                # 加仓识别: 同标的同方向已有批次 → 加仓路径(不占新标的名额)
                existing = next((p for p in self.executor.positions() if p.symbol == sym and p.direction == side), None)
                batches_used = existing.batches if existing else 0
                existing_pnl_pct, existing_entry = 0.0, 0.0
                if existing is not None:
                    try:
                        q2 = self.bg.quote(sym)
                        last2 = float(q2.get("lastPr", 0) or 0)
                        if existing.direction == "long":
                            existing_pnl_pct = (last2 - existing.avg_entry) / existing.avg_entry * 100
                        else:
                            existing_pnl_pct = (existing.avg_entry - last2) / existing.avg_entry * 100
                        existing_entry = existing.avg_entry
                    except Exception:
                        pass
                # 日线方向门控(代码即法律): 日线逆势禁做多/禁做空
                # 例外: 深跌40%+且企稳的热门票允许 BUY(用户场景 2026-09, 低风险尝试)
                if side == "long" and inp.deep_dip:
                    ok_dir, dir_reason = True, f"深跌反转信号例外放行({inp.deep_dip})"
                elif side == "long" and getattr(self, "_market_down", False):
                    ok_dir, dir_reason = False, (f"大盘日线向下({self._market_note}), 禁开多仓"
                                                 f"(空单/深跌反转例外)")
                else:
                    ok_dir, dir_reason = self.risk.validate_daily_direction(
                        inp.daily_regime, inp.daily_adx, side)
                if not ok_dir:
                    self.memory.record_hold(sym, f"REJECT: {dir_reason}", session)
                    log.warning("拒绝 %s: %s", sym, dir_reason)
                    continue
                price = float(quote.get("lastPr", 0))
                contract = self._contract(sym)
                ok, reason, params = self.risk.validate_open(
                    sym, price, side, decision.stop_loss, decision.take_profit,
                    contract, account,
                    int(account.get("long_count", 0)), int(account.get("short_count", 0)),
                    batches_used=batches_used,
                    existing_pnl_pct=existing_pnl_pct,
                    existing_entry=existing_entry)
                if not ok:
                    self.memory.record_hold(sym, f"REJECT: {reason}", session)
                    log.warning("拒绝 %s: %s", sym, reason)
                    continue
                try:
                    pos = self.executor.open(sym, params, quote)
                except Exception as e:
                    log.error("开仓失败 %s: %s", sym, str(e)[:100])
                    self.memory.record_hold(sym, f"OPEN_FAIL: {str(e)[:80]}", session)
                    continue
                action_label = "BUY" if side == "long" else "SELL"
                self.memory.record_open(
                    sym, action_label, pos.avg_entry, float(params["stop_loss"]),
                    float(params["take_profit"]), decision.reason, session,
                    params={"notional": params["notional"], "leverage": params["leverage"],
                            "direction": side})
                log.info("✅ %s %s @$%.4f SL=%.2f TP=%.2f RR=%.2f | %s",
                         "开多" if side == "long" else "开空", sym, pos.avg_entry,
                         params["stop_loss"], params["take_profit"], params["rr"],
                         decision.reason[:60])
            else:
                self.memory.record_hold(sym, decision.reason or "AI HOLD", session)
                log.info("HOLD %s | %s", sym, (decision.reason or "")[:60])

    # 注: 复盘/教训循环已按用户要求移除(2026-09)——
    # AI 决策不进 lessons 注入, 只保留交易记录与品种历史(get_symbol_history)。

    # ---------- 主循环 ----------
    def run_once(self) -> dict[str, Any]:
        t0 = time.time()
        self._load_contracts()
        account = self._account()
        self.risk.refresh_day(float(account.get("equity", 0)))
        self._reconcile()  # 启动残留清理
        # 行情驱动(paper 关键): 先给持仓喂最新报价, 再 tick 触发交易所侧 TPSL/资金费率结算。
        # (此前引擎从不调 tick → paper 的 SL/TP 穿价与 funding 结算从未发生 — 已修复)
        try:
            for p in self.executor.positions():
                q = self.bg.quote(p.symbol)
                if q:
                    self.executor.set_quote(p.symbol, q)
            if hasattr(self.executor, "tick"):
                self.executor.tick()
        except Exception as e:
            log.warning("行情驱动 tick 异常: %s", str(e)[:100])
        # tick 平掉的仓立即补录(不拖到下一轮): 防 crash 丢失 + 复盘即时
        self._reconcile()
        self._ensure_protection()
        self._manage_positions(account)
        self._scan(account)
        snap = self._snapshot(account)
        self._write_snapshot(snap)
        log.info("本轮完成 (%.1fs) 引擎状态: %s", time.time() - t0,
                 json.dumps(snap, ensure_ascii=False))
        return snap

    def _snapshot(self, account: dict) -> dict[str, Any]:
        return {
            "ts": time.time(),
            "mode": self.executor.name,
            "equity": round(float(account.get("equity", 0)), 4),
            "available": round(float(account.get("available", 0)), 4),
            "notional": round(float(account.get("notional", 0)), 1),
            "positions": [p.symbol for p in self.executor.positions()],
            "risk": self.risk.snapshot(),
            "mem": self.memory.stats(),
        }

    def _write_snapshot(self, snap: dict) -> None:
        tmp = self.state_dir / "trader_state.json"
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="supermarket", description="Bitget 超市策略 AI 交易系统")
    ap.add_argument("--mode", choices=["paper", "live"], default=None)
    ap.add_argument("--once", action="store_true", help="跑一轮后退")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    if args.mode:
        cfg.mode = args.mode
    # 归一化: live 视为 real(service 文件用 --mode live; 只认 "real" 会让实盘跑成纸面)
    if str(cfg.mode).lower() in ("live", "real"):
        cfg.mode = "real"
    if args.interval:
        cfg.scan_interval = args.interval

    # 实盘安全门
    if cfg.mode == "real":
        if os.environ.get("LIVE_CONFIRM", "no") != "yes":
            sys.exit("实盘模式需要环境变量 LIVE_CONFIRM=yes(显式确认)。"
                     "且需账户已充值。仅在明确同意实盘时使用。")

    log.info("=== bitget-supermarket-trader v%s mode=%s ===", __version__, cfg.mode)
    eng = SupermarketEngine(cfg)
    if cfg.mode == "real":
        try:
            from supermarket import notify
            acc0 = eng._account()
            pos0 = eng.executor.positions()
            notify.send(
                "🟢 超市实盘服务已启动"
                f"\n权益 ${float(acc0.get('equity', 0)):.2f} | 持仓 {len(pos0)} 个"
                f"{'（' + ','.join(p.symbol for p in pos0) + '）' if pos0 else ''}"
                f"\n策略: 进货四道门 / 有盈利就卖 / 结构未坏拿住 / 天气门"
                f"\n发送 /help 查看指令")
        except Exception:
            pass
        try:
            import threading
            from supermarket.telegram_bot import TelegramCommander, install_menu
            install_menu()
            threading.Thread(target=TelegramCommander(eng).run_forever,
                             daemon=True, name="tg-commander").start()
        except Exception as e:
            log.warning("Telegram 指令线程启动失败(不影响交易): %s", str(e)[:100])

    if not cfg.bitget.ready:
        sys.exit("Bitget 密钥未配置 (config.yaml → .env 的 BITGET_* 变量)")

    if cfg.mode == "real":
        acc = eng._account()
        eq_now = float(acc.get("equity", 0))
        if eq_now < 1.0:
            sys.exit(f"实盘账户余额不足 $1(当前 {eq_now:.8f}), 禁止启动. "
                     f"请先充值 $20~50(尘埃值不算余额)。")

    if args.once:
        eng.run_once()
        return

    from supermarket.market import us_session
    last_skip_log = 0.0
    while True:
        try:
            # 非交易日/非交易时段门: 周末停机(中线策略不需要周末盯盘, TPSL在交易所保护)
            if cfg.skip_weekend and us_session().startswith("weekend"):
                now = time.time()
                if now - last_skip_log > 3600:  # 每小时只记一次
                    log.info("周末停机中(美股休市), 持仓由交易所侧 TPSL 保护")
                    last_skip_log = now
                time.sleep(600)
                continue
            eng.run_once()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("本轮异常: %s", str(e)[:150])
        time.sleep(cfg.scan_interval)


if __name__ == "__main__":
    main()