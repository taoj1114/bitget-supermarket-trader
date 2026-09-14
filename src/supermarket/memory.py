"""AI 记忆层(持久化): 决策日志 / HOLD 审计 / 教训 / 品种历史 / 复盘基线。

分离设计(来自 ai-native-trading skill 教训):
- decisions[]: 只存开仓动作(BUY), 已平仓的补 outcome; HOLD 不进 decisions
- holds[]: 独立上限200, 只审计不复盘
- decisions[]: 开仓决策(含 outcome/pnl/close_reason, 复盘审计用)
- holds[]: HOLD/拒绝记录(近MAX_HOLDS条)
(注: 复盘/教训循环已移除, lessons 字段保留空以供将来兼容)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_DECISIONS = 500
MAX_HOLDS = 200
MAX_LESSONS = 8


class AIMemory:
    def __init__(self, state_dir: Path):
        self._file = Path(state_dir) / "ai_memory.json"
        self.decisions: list[dict] = []
        self.holds: list[dict] = []
        self.lessons: list[str] = []
        self.review_base: int = 0
        self._load()

    def _load(self) -> None:
        try:
            if self._file.exists():
                d = json.loads(self._file.read_text())
                self.decisions = d.get("decisions", [])
                self.holds = d.get("holds", [])
                self.lessons = d.get("lessons", [])
                self.review_base = int(d.get("review_base", 0))
        except Exception as e:
            log.warning("ai_memory.json 读取失败: %s", str(e)[:80])

    def _save(self) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "decisions": self.decisions[-MAX_DECISIONS:],
            "holds": self.holds[-MAX_HOLDS:],
            "lessons": self.lessons[-MAX_LESSONS:],
            "review_base": self.review_base,
        }, ensure_ascii=False, indent=1))
        tmp.replace(self._file)

    # ---------- 写入 ----------
    def record_open(self, symbol: str, action: str, entry: float, sl: float, tp: float,
                    reason: str, session: str, params: dict | None = None) -> None:
        self.decisions.append({
            "ts": time.time(), "symbol": symbol, "action": action, "entry": entry,
            "sl": sl, "tp": tp, "reason": reason, "session": session,
            "outcome": None, "close_ts": None, "close_price": None, "pnl": None,
            "close_reason": None, "max_pnl_pct": 0.0,
            "params": params or {},
        })
        self._save()

    def record_hold(self, symbol: str, reason: str, session: str) -> None:
        self.holds.append({"ts": time.time(), "symbol": symbol, "reason": reason, "session": session})
        self._save()

    def close_decision(self, symbol: str, close_price: float, pnl: float,
                       close_reason: str, max_pnl_pct: float = 0.0) -> bool:
        """按 symbol 找到未平仓的 decision 补 outcome。返回是否找到。"""
        for d in reversed(self.decisions):
            if d["symbol"] == symbol and d.get("outcome") is None and d.get("entry"):
                d["outcome"] = "closed"
                d["close_ts"] = time.time()
                d["close_price"] = close_price
                d["pnl"] = round(pnl, 6)
                d["close_reason"] = close_reason
                d["max_pnl_pct"] = round(max(max_pnl_pct, 0.0), 4)
                self._save()
                return True
        return False

    def set_max_pnl(self, symbol: str, max_pnl_pct: float) -> None:
        for d in reversed(self.decisions):
            if d["symbol"] == symbol and d.get("outcome") is None:
                cur = float(d.get("max_pnl_pct", 0))
                if max_pnl_pct > cur + 0.02:  # 节流: 变化>0.02% 才落盘
                    d["max_pnl_pct"] = round(max_pnl_pct, 4)
                    self._save()
                return

    # ---------- 读取 ----------
    def open_decisions(self) -> list[dict]:
        return [d for d in self.decisions if d.get("outcome") is None and d.get("entry")]

    def closed_decisions(self) -> list[dict]:
        return [d for d in self.decisions if d.get("outcome") is not None]

    def get_symbol_history(self, symbol: str, limit: int = 3) -> str:
        """该股已平仓结果(防锚定: 只注入 outcome 已定的)。"""
        rows = [d for d in reversed(self.decisions)
                if d["symbol"] == symbol and d.get("outcome") is not None][:limit]
        if not rows:
            return ""
        lines = []
        for h in rows:
            lines.append(
                f"  {time.strftime('%m-%d %H:%M', time.localtime(h['ts']))} "
                f"{h['action']} → {h['close_reason']} pnl=${h.get('pnl', 0):+.3f} "
                f"| {h['reason'][:40]}"
            )
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        closed = self.closed_decisions()
        wins = [d for d in closed if d.get("pnl", 0) > 0]
        return {
            "open": len(self.open_decisions()),
            "closed": len(closed),
            "wins": len(wins),
            "pnl": sum(float(d.get("pnl", 0)) for d in closed),
            "holds": len(self.holds),
            "lessons": len(self.lessons),
        }