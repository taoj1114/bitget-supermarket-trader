"""主引擎: 账户 → 对账 → 管仓 → 扫描开仓 → 复盘, 5分钟一跳。

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
    SYSTEM_REVIEW,
    build_manage_prompt,
    build_open_prompt,
    build_review_prompt,
    committee_diagnosis,
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
        self.state_dir = cfg.state_path
        self.bg = BitgetClient(cfg.bitget.base_url, cfg.bitget.api_key,
                               cfg.bitget.secret, cfg.bitget.passphrase)
        self.market = MarketData(self.bg)
        self.provider = build_provider(cfg)
        self.risk = RiskEngine(cfg, self.state_dir)
        self.memory = AIMemory(self.state_dir)
        if cfg.mode == "real":
            self.executor: Any = RealExecutor(self.bg, cfg, self.state_dir)
        else:
            self.executor = PaperExecutor(cfg, self.state_dir)
        self._contracts: dict[str, dict] = {}
        self._scan_rotate = 0
        self._last_daily: dict[str, float] = {}

    # ---------- 合约池 ----------
    def _load_contracts(self) -> None:
        try:
            for c in self.bg.stock_contracts():
                self._contracts[c["symbol"]] = c
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
            return self.executor.account()
        except Exception as e:
            log.error("账户快照失败: %s", str(e)[:100])
            return {"equity": 0, "available": 0, "notional": 0, "position_count": 0,
                    "day_pnl": 0, "mode": self.executor.name}

    # ---------- 1. 对账 ----------
    def _reconcile(self) -> None:
        """本地 open decisions 与实际持仓比对; 交易所/纸面已平的仓 → 补录。"""
        held = {p.symbol for p in self.executor.positions()}
        for d in self.memory.open_decisions():
            sym = d["symbol"]
            if sym in held:
                continue
            # 已被外部平掉(交易所 SL/TP 或纸面穿价)
            price = 0.0
            try:
                price = float(self.bg.quote(sym).get("lastPr", 0))
            except Exception:
                pass
            entry = float(d.get("entry", 0))
            if entry > 0 and price > 0:
                pnl = (price - entry) / entry * float((d.get("params") or {}).get("notional", 0))
            else:
                pnl = 0.0
            self.memory.close_decision(sym, price, pnl, "EXCHANGE_SLTP(对账补录)",
                                       max_pnl_pct=float(d.get("max_pnl_pct", 0)))
            self.risk.on_close(pnl)
            log.info("对账: %s 已被外部平仓, 补录 pnl $%.4f", sym, pnl)

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
                inp = self.market.build_input(sym, quote, account,
                                              lessons=lessons, manage=True)
                decision = self.provider.decide_manage(
                    SYSTEM_MANAGE, build_manage_prompt(inp, {
                        "symbol": sym, "avg_entry": entry, "notional": pos.notional,
                        "unrealized_pnl_pct": (last - entry) / entry * 100 if entry else 0,
                    }))
                return (pos, quote, decision)
            except Exception as e:
                log.error("管仓 %s 决策异常: %s", sym, str(e)[:80])
                return None

        results = []
        if len(positions) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(positions))) as ex:
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

    # ---------- 3. 扫描开仓 ----------
    def _pick_candidates(self, account: dict) -> list[str]:
        held = {p.symbol for p in self.executor.positions()}
        try:
            tickers = self.market.tickers()
        except Exception as e:
            log.error("ticker 获取失败: %s", str(e)[:80])
            return []
        vol = {t["symbol"]: float(t.get("usdtVolume", 0) or 0) for t in tickers}
        # 流动性门槛(ceiling: 保留全部经 hot 白名单)
        valid = [s for s in vol
                 if s in self._contracts and s not in held
                 and (vol[s] >= self.cfg.min_turnover_floor or s in set(self.cfg.hot_symbols))]
        hot = [s for s in self.cfg.hot_symbols if s in valid]
        rest = sorted((s for s in valid if s not in hot), key=lambda s: -vol[s])
        out = hot[:3]
        n = self.cfg.max_symbols_per_round - len(out)
        if n > 0 and rest:
            rotated = rest[self._scan_rotate:] + rest[:self._scan_rotate]
            out += rotated[:n]
            self._scan_rotate = (self._scan_rotate + n) % max(len(rest), 1)
        return out

    def _scan(self, account: dict) -> None:
        if account.get("equity", 0) <= 0:
            log.warning("净值0, 跳过扫描")
            return
        paused = self.risk.paused()
        if paused:
            log.info("熔断: %s", paused)
            return
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
                                              history=history, lessons=lessons)
                decision = self.provider.decide_open(SYSTEM_OPEN, build_open_prompt(inp))
                return (sym, quote, inp, decision)
            except Exception as e:
                log.error("扫描 %s 异常: %s", sym, str(e)[:80])
                return None

        results = []
        if len(candidates) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as ex:
                results = [r for r in ex.map(probe, candidates) if r]
        else:
            r = probe(candidates[0]) if candidates else None
            results = [r] if r else []

        # 串行执行阶段(风控校验+开仓, 防并发超仓)
        for sym, quote, inp, decision in results:
            self.executor.set_quote(sym, quote)
            if decision.is_buy:
                # 日线方向门控(代码即法律): 日线逆势禁做多
                ok_dir, dir_reason = self.risk.validate_daily_direction(
                    inp.daily_regime, inp.daily_adx)
                if not ok_dir:
                    self.memory.record_hold(sym, f"REJECT: {dir_reason}", session)
                    log.warning("拒绝 %s: %s", sym, dir_reason)
                    continue
                price = float(quote.get("lastPr", 0))
                contract = self._contract(sym)
                ok, reason, params = self.risk.validate_open(
                    sym, price, decision.stop_loss, decision.take_profit,
                    contract, account, account.get("position_count", 0))
                if not ok:
                    self.memory.record_hold(sym, f"REJECT: {reason}", session)
                    log.warning("拒绝 %s: %s", sym, reason)
                    continue
                try:
                    pos = self.executor.open(sym, params, quote)
                except Exception as e:
                    log.error("开仓失败 %s: %s", sym, str(e)[:100])
                    continue
                self.memory.record_open(
                    sym, "BUY", pos.avg_entry, float(params["stop_loss"]),
                    float(params["take_profit"]), decision.reason, session,
                    params={"notional": params["notional"], "leverage": params["leverage"]})
                log.info("✅ 开仓 %s @$%.4f SL=%.2f TP=%.2f RR=%.2f | %s",
                         sym, pos.avg_entry, params["stop_loss"],
                         params["take_profit"], params["rr"], decision.reason[:60])
            else:
                self.memory.record_hold(sym, decision.reason or "AI HOLD", session)
                log.info("HOLD %s | %s", sym, (decision.reason or "")[:60])

    # ---------- 4. 复盘 ----------
    def _maybe_review(self) -> None:
        if not self.memory.review_due():
            return
        closed = self.memory.closed_decisions()
        diag = committee_diagnosis(closed)
        lessons = self.provider.review(SYSTEM_REVIEW, build_review_prompt(closed, diag))
        self.memory.save_lessons(lessons)
        self.memory.set_review_base(len(closed))
        log.info("复盘完成: %d 条教训 (%s)", len(lessons), diag[:80])

    # ---------- 主循环 ----------
    def run_once(self) -> dict[str, Any]:
        t0 = time.time()
        self._load_contracts()
        account = self._account()
        self.risk.refresh_day(float(account.get("equity", 0)))
        self._reconcile()
        self._manage_positions(account)
        self._scan(account)
        self._maybe_review()
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
    if args.interval:
        cfg.scan_interval = args.interval

    # 实盘安全门
    if cfg.mode == "real":
        if os.environ.get("LIVE_CONFIRM", "no") != "yes":
            sys.exit("实盘模式需要环境变量 LIVE_CONFIRM=yes(显式确认)。"
                     "且需账户已充值。仅在明确同意实盘时使用。")

    log.info("=== bitget-supermarket-trader v%s mode=%s ===", __version__, cfg.mode)
    eng = SupermarketEngine(cfg)

    if not cfg.bitget.ready:
        sys.exit("Bitget 密钥未配置 (config.yaml → .env 的 BITGET_* 变量)")

    if cfg.mode == "real":
        acc = eng._account()
        if float(acc.get("equity", 0)) <= 0:
            sys.exit("实盘账户余额为 0, 禁止启动. 请先充值。")

    if args.once:
        eng.run_once()
        return

    while True:
        try:
            eng.run_once()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("本轮异常: %s", str(e)[:150])
        time.sleep(cfg.scan_interval)


if __name__ == "__main__":
    main()