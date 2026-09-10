"""风控引擎: 代码硬约束 = 法律。LLM 的决策是建议, 这里决定能否执行。

校验矩阵(见 DETAILED_DESIGN §5):
- 名义敞口 ≤ 净值×6            (全仓爆仓线 ≈ -16%)
- 仓数 ≤ max(1, floor(净值/10)) (保证金占用 ≤ 净值60%)
- SL 必填、方向正确、距离 1%~12%、RR≥1.5
- 日亏损 ≥30% 净值 → 当日禁开新仓; 连亏3 → 暂停2小时
状态持久化到 state/breakers.json, 重启不丢。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BreakerState:
    day_date: str = ""
    day_initial_equity: float = 0.0
    day_pnl: float = 0.0
    consecutive_losses: int = 0
    paused_until: float = 0.0
    paused_reason: str = ""


class RiskEngine:
    def __init__(self, cfg, state_dir: Path):
        self.cfg = cfg
        self.state = BreakerState()
        self._sfile = Path(state_dir) / "breakers.json"
        self._load()
        self.rejects: list[dict] = []

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            if self._sfile.exists():
                d = json.loads(self._sfile.read_text())
                self.state = BreakerState(**{k: d.get(k, getattr(self.state, k))
                                              for k in self.state.__dict__})
        except Exception as e:
            log.warning("breakers.json 读取失败, 重置: %s", str(e)[:80])

    def _save(self) -> None:
        tmp = self._sfile.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.__dict__, ensure_ascii=False, indent=1))
        tmp.replace(self._sfile)

    # ---------- 日基线 ----------
    def refresh_day(self, equity: float) -> None:
        today = date.today().isoformat()
        if self.state.day_date != today:
            self.state.day_date = today
            self.state.day_initial_equity = equity if equity > 0 else 0.0
            self.state.day_pnl = 0.0
            self._save()
            log.info("新交易日基线: 净值 $%.2f", self.state.day_initial_equity)

    # ---------- 熔断门 ----------
    def paused(self) -> str:
        """返回暂停原因, 未暂停返回空串。"""
        if time.time() < self.state.paused_until:
            remain = int((self.state.paused_until - time.time()) / 60)
            return f"{self.state.paused_reason}(剩余{remain}分钟)"
        if self.state.paused_until and time.time() >= self.state.paused_until:
            self.state.paused_until = 0.0
            self.state.paused_reason = ""
            self.state.consecutive_losses = 0
            self._save()
            log.info("暂停到期, 自动恢复")
        return ""

    def _day_drawdown_hit(self, equity: float) -> bool:
        base = self.state.day_initial_equity
        if base <= 0 or equity >= base:
            return False
        return (base - equity) / base * 100 >= self.cfg.max_daily_drawdown_pct

    # ---------- 开仓校验 ----------
    def validate_daily_direction(self, daily_regime: str, daily_adx: float) -> tuple[bool, str]:
        """日线方向门控(用户铁律: 永不逆势): 日线明确向下(ADX≥25)→ 禁做多。"""
        if daily_regime == "trend_down" and daily_adx >= 25:
            return False, f"日线逆势: regime={daily_regime} ADX={daily_adx:.1f}≥25, 禁做多(永不逆势)"
        return True, "ok"

    def validate_open(self, symbol: str, price: float, sl: float | None, tp: float | None,
                      contract: dict[str, Any], account: dict[str, Any], n_positions: int) -> tuple[bool, str, dict]:
        """返回 (ok, reason, 下单参数)。只做多。"""
        self.rejects.clear()
        equity = float(account.get("equity", 0))
        notional_now = float(account.get("notional", 0))

        if equity <= 0:
            return False, f"账户净值为0, 无法开仓({symbol})", {}
        p = self.paused()
        if p:
            return False, f"熔断中: {p}", {}
        if self._day_drawdown_hit(equity):
            return False, f"当日回撤≥{self.cfg.max_daily_drawdown_pct:.0f}%, 停止新开仓", {}

        lev = min(self.cfg.leverage, int(contract.get("maxLever", 20)))
        margin = min(self.cfg.margin_per_trade_usd, equity * 0.6)
        notional = margin * lev
        min_usdt = float(contract.get("minTradeUSDT", 5))
        if notional < min_usdt:
            notional = min_usdt  # 至少满足最小名义
            margin = notional / lev
        if notional <= 0:
            return False, f"名义≤0, 无法开仓({symbol})", {}

        # 名义总量
        if notional_now > 0 and (notional_now + notional) > equity * self.cfg.max_notional_mult:
            cap = equity * self.cfg.max_notional_mult - notional_now
            return False, (f"名义超限: 已有${notional_now:.0f}+新${notional:.0f}"
                           f">净值×{self.cfg.max_notional_mult:.0f}=${equity * self.cfg.max_notional_mult:.0f}, 剩余额度${cap:.0f}"), {}
        # 仓数
        max_pos = max(1, int(equity // self.cfg.max_positions_divisor))
        if n_positions >= max_pos:
            return False, f"仓数已达上限 {max_pos}(净值${equity:.0f}/{self.cfg.max_positions_divisor})", {}

        # SL/TP 距离与盈亏比
        if sl is None or sl <= 0:
            return False, "AI未提供止损价, 拒绝开仓(止损是超市底线)", {}
        if not (sl < price):
            return False, f"止损价应低于买入价(BUY仓): sl={sl} price={price}", {}
        sl_dist = (price - sl) / price * 100
        if sl_dist < self.cfg.sl_min_pct:
            return False, f"止损过近({sl_dist:.2f}% < {self.cfg.sl_min_pct}%), 噪音止损, 拒绝", {}
        if sl_dist > self.cfg.sl_max_pct:
            return False, f"止损过远({sl_dist:.2f}% > {self.cfg.sl_max_pct}%), 失控, 拒绝", {}

        tp_eff = tp if (tp and tp > price) else None
        if tp_eff is None:
            return False, "AI未提供止盈价, 拒绝开仓(超市要快进快出)", {}
        tp_dist = (tp_eff - price) / price * 100
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < self.cfg.min_rr:
            return False, f"盈亏比不足: TP{tp_dist:.1f}%/SL{sl_dist:.1f}% = {rr:.2f} < {self.cfg.min_rr}, 拒绝", {}

        # 数量步长
        mult = float(contract.get("sizeMultiplier", 0.01))
        qty_raw = notional / price
        qty = max(mult, round(qty_raw / mult) * mult)
        if qty * price < min_usdt:
            qty = mult

        params = {
            "symbol": symbol, "size": qty, "notional": qty * price,
            "margin": qty * price / lev, "leverage": lev,
            "stop_loss": sl, "take_profit": tp_eff,
            "sl_dist_pct": sl_dist, "tp_dist_pct": tp_dist, "rr": rr,
            "volume_place": int(contract.get("volumePlace", 4) or 4),  # 数量小数位(下单精度)
        }
        return True, "ok", params

    # ---------- 平仓后统计 ----------
    def on_close(self, pnl: float) -> None:
        self.state.day_pnl += pnl
        if pnl <= 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.state.paused_until = time.time() + self.cfg.pause_after_loss_minutes * 60
                self.state.paused_reason = f"连续{self.state.consecutive_losses}次亏损"
                log.warning("连亏%d次, 暂停%d分钟", self.state.consecutive_losses,
                            self.cfg.pause_after_loss_minutes)
        else:
            self.state.consecutive_losses = 0
        self._save()

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.state.__dict__,
            "paused": self.paused() != "",
            "day_drawdown_pct": (
                (self.state.day_initial_equity - (self.state.day_initial_equity + self.state.day_pnl))
                / self.state.day_initial_equity * 100 if self.state.day_initial_equity else 0.0
            ),
        }