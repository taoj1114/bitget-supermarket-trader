"""风控引擎: 代码硬约束 = 法律。LLM 的决策是建议, 这里决定能否执行。

风控矩阵(见 DETAILED_DESIGN §5):
- 名义敞口 ≤ 净值×6            (全仓爆仓线 ≈ -16%)
- 多头仓数 = min(6, floor(净值×6/每仓名义))  (超市: 50刀账户6仓位)
- 空头仓数 ≤ 2                 (对冲配额)
- SL 必填、方向正确、距离 2%~15%、RR≥1.5(临界1.0~1.5自动修正TP)
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
    def validate_daily_direction(self, daily_regime: str, daily_adx: float,
                                 side: str = "long") -> tuple[bool, str]:
        """日线方向门控(用户铁律: 永不逆势):
        多头: 日线明确向下(ADX≥25)→ 禁做多; 空头: 日线明确向上(ADX≥25)→ 禁做空。"""
        if side == "long" and daily_regime == "trend_down" and daily_adx >= 25:
            return False, f"日线逆势: regime={daily_regime} ADX={daily_adx:.1f}≥25, 禁做多(永不逆势)"
        if side == "short" and daily_regime == "trend_up" and daily_adx >= 25:
            return False, f"日线逆势: regime={daily_regime} ADX={daily_adx:.1f}≥25, 禁做空(永不逆势)"
        return True, "ok"

    def validate_open(self, symbol: str, price: float, side: str,
                      sl: float | None, tp: float | None,
                      contract: dict[str, Any], account: dict[str, Any],
                      long_count: int, short_count: int,
                      batches_used: int = 0,
                      existing_pnl_pct: float = 0.0,
                      existing_entry: float = 0.0,
                      leverage: int | None = None,
                      margin_usd: float | None = None) -> tuple[bool, str, dict]:
        """返回 (ok, reason, 下单参数)。side: long(做多) / short(做空)。

        leverage: AI 决定的风险预算(2026-09-19 用户要求: 按行情调节杠杆控制风险)。
        名义 = 保证金 × 杠杆, 故杠杆=单笔风险敞口大小; 程序只做范围钳制(不替AI决策)。
        """
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

        # 杠杆范围钳制: AI 给值则在 [cfg.leverage_min, min(cfg.leverage, 合约maxLever)] 内取用;
        # 未给则用上限(=旧行为, 向后兼容); 低于下限抬到下限(保证名义≥交易所最低), 高于上限压到上限。
        max_lev = min(self.cfg.leverage, int(contract.get("maxLever", 20)))
        min_lev = max(1, min(self.cfg.leverage_min, max_lev))
        if leverage:
            lev = max(min_lev, min(max_lev, int(leverage)))
        else:
            lev = max_lev
        # 仓位大小由 AI 决定(2026-09-22 用户要求): 钳制 [margin_min, margin_max], 未给用默认 $2;
        # 批次>0(加仓)时不超过首批(越加越轻, 递减补货)
        if margin_usd:
            margin = max(self.cfg.margin_min_usd, min(self.cfg.margin_max_usd, float(margin_usd)))
        else:
            margin = self.cfg.margin_per_trade_usd
        if batches_used > 0 and margin > self.cfg.margin_per_trade_usd:
            margin = self.cfg.margin_per_trade_usd   # 加仓默认不超过首批标准(防越加越重)
        margin = min(margin, equity * 0.6)
        notional = margin * lev
        min_usdt = float(contract.get("minTradeUSDT", 5))
        if notional < min_usdt:
            notional = min_usdt  # 至少满足最小名义
            margin = notional / lev
        if notional <= 0:
            return False, f"名义≤0, 无法开仓({symbol})", {}

        # 名义总量(多+空合计)
        if notional_now > 0 and (notional_now + notional) > equity * self.cfg.max_notional_mult:
            cap = equity * self.cfg.max_notional_mult - notional_now
            return False, (f"名义超限: 已有${notional_now:.0f}+新${notional:.0f}"
                           f">净值×{self.cfg.max_notional_mult:.0f}=${equity * self.cfg.max_notional_mult:.0f}, 剩余额度${cap:.0f}"), {}

        # 仓数(多头由 净值×6÷每仓名义 推导, 硬顶6; 空头独立上限2)
        # 分批建仓: 同一标的同方向最多3批(超市补货); 加仓不占用新标的名额
        if batches_used >= self.cfg.max_batches_per_symbol:
            return False, (f"该标的方向 {side} 已达批次上限 {self.cfg.max_batches_per_symbol}"
                           f"(${self.cfg.margin_per_trade_usd*self.cfg.max_batches_per_symbol:.0f}保证金/标的), 不再加仓"), {}
        if batches_used > 0:
            # 金字塔补货: 只在浮盈时补(好卖的货才进货); 浮亏禁止摊平(赌徒行为)
            if existing_pnl_pct < 0.5:
                return False, (f"补货需浮盈≥0.5%(当前{existing_pnl_pct:+.2f}%)——"
                               f"只补好卖的货, 浮亏不摊平(超市纪律)"), {}
            if existing_entry > 0 and sl is not None and sl > 0 and side == "long" and sl < existing_entry * 0.9985:
                return False, (f"补货后止损({sl:.2f})应≥原均价+0.25%({existing_entry*1.0025:.2f})"
                               f"——补货即保护, 最差结果必须是不亏"), {}
        if batches_used == 0:
            if side == "long":
                # $50账户 → floor(50×6/40)=7 → 用户指定上限6; $30 → 4
                max_pos = min(6, max(1, int(equity * self.cfg.max_notional_mult /
                                            (self.cfg.margin_per_trade_usd * lev))))
                if long_count >= max_pos:
                    return False, f"多头仓数已达上限 {max_pos}(${equity:.0f}账户, 用户设定6)", {}
            else:
                if short_count >= self.cfg.max_short_positions:
                    return False, f"空头仓数已达上限 {self.cfg.max_short_positions}(用户设定: 对冲用一两个)", {}

        # SL/TP 距离与盈亏比(方向镜像)
        if sl is None or sl <= 0:
            return False, "AI未提供止损价, 拒绝开仓(止损是超市底线)", {}
        if side == "long":
            if not (sl < price):
                return False, f"止损价应低于买入价(多头): sl={sl} price={price}", {}
            sl_dist = (price - sl) / price * 100
            tp_eff = tp if (tp and tp > price) else None
        else:
            if not (sl > price):
                return False, f"止损价应高于卖出价(空头): sl={sl} price={price}", {}
            sl_dist = (sl - price) / price * 100
            tp_eff = tp if (tp and 0 < tp < price) else None
        if sl_dist < self.cfg.sl_min_pct:
            return False, f"止损过近({sl_dist:.2f}% < {self.cfg.sl_min_pct}%), 噪音止损, 拒绝", {}
        sl_max = self.cfg.sl_max_pct
        if sl_dist > sl_max:
            return False, f"止损过远({sl_dist:.2f}% > {sl_max}%), 失控, 拒绝", {}

        if tp_eff is None:
            return False, "AI未提供止盈价, 拒绝开仓(超市要快进快出)", {}
        tp_dist = abs(tp_eff - price) / price * 100
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < self.cfg.min_rr:
            if rr >= 1.0:
                # 临界不足(如1.49 vs 1.5): 自动修正 TP 至最低盈亏比, 保留 AI 意图
                fixed_tp_dist = sl_dist * self.cfg.min_rr
                if side == "long":
                    tp_eff = price * (1 + fixed_tp_dist / 100)
                else:
                    tp_eff = price * (1 - fixed_tp_dist / 100)
                tp_dist = fixed_tp_dist
                rr = self.cfg.min_rr
                log.info("盈亏比%.2f略低于%.1f → TP 自动修正至 RR=%.1f (保留仓位)",
                         tp_dist / sl_dist, self.cfg.min_rr, self.cfg.min_rr)
            else:
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
            "direction": side,
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