"""短期样本隔离测试(2026-09-29 用户: 去掉中期经验数据, 避免干扰短期交易)。

隔离规则: 2026-09-22 00:00 CST(短期化 v3.0 上线)之前的成交属"中期时代"
(宽止损 3~15%/持数日), 不进入 ①自迭代统计 ②AI 的历史交易输入 —— 但不删档(审计需要)。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import supermarket.self_tune as st
from supermarket.memory import AIMemory

DEFAULT_SINCE = st.short_term_since()
OLD = DEFAULT_SINCE - 86400 * 10      # 中期时代(10 天前)
NEW = DEFAULT_SINCE + 3600            # 短期时代


def _mem_with(tmp: Path, rows: list[dict]) -> AIMemory:
    (tmp / "ai_memory.json").write_text(
        __import__("json").dumps({"decisions": rows, "holds": [], "lessons": [], "review_base": 0}))
    return AIMemory(tmp)


def _row(sym: str, ts: float, pnl: float, reason: str = "中期大亏单") -> dict:
    return {"ts": ts, "symbol": sym, "action": "BUY", "entry": 100.0, "sl": 90.0, "tp": 110.0,
            "reason": reason, "session": "regular", "outcome": "closed", "close_ts": ts + 3600,
            "close_price": 101.0, "pnl": pnl, "close_reason": "SL", "max_pnl_pct": 0.5, "params": {}}


def test_evidence_excludes_legacy_samples():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        st.STATE_DIR = tmp            # 让 short_term_since 读临时目录(无覆盖文件 → 默认值)
        mem = _mem_with(tmp, [
            _row("OLD1USDT", OLD, -3.00), _row("OLD2USDT", OLD - 100, -2.50),   # 中期
            _row("NEW1USDT", NEW, +0.20), _row("NEW2USDT", NEW + 100, -0.10),   # 短期
        ])
        ev = st.collect_evidence(mem)
        assert ev["total"] == 2, f"只应统计短期样本, 实际 {ev['total']}"
        assert ev["excluded_legacy"] == 2
        assert abs(ev["overview"]["pnl"] - 0.10) < 1e-6, "中期大亏不得进入统计"


def test_symbol_history_excludes_legacy():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mem = _mem_with(tmp, [
            _row("INTCUSDT", OLD, -0.90, "中期: 宽止损持三天"),
            _row("INTCUSDT", NEW, +0.25, "短期: 反转后顺势"),
        ])
        hist = mem.get_symbol_history("INTCUSDT")
        assert "反转后顺势" in hist
        assert "中期: 宽止损持三天" not in hist, "中期记录不得进入 AI 输入"


def test_since_override_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        st.STATE_DIR = tmp
        (tmp / "short_term_since.json").write_text('{"ts": %d}' % int(NEW - 10))
        mem = _mem_with(tmp, [_row("XUSDT", NEW - 100, -1.0), _row("XUSDT", NEW + 100, +1.0)])
        ev = st.collect_evidence(mem)
        assert ev["total"] == 1 and ev["excluded_legacy"] == 1


def test_default_since_is_short_term_v3_launch():
    """默认起点必须是 2026-09-22 00:00 CST(短期化 v3.0), 不能漂移。"""
    import datetime as dt
    d = dt.datetime.fromtimestamp(DEFAULT_SINCE, tz=dt.timezone(dt.timedelta(hours=8)))
    assert (d.year, d.month, d.day) == (2026, 9, 22) and d.hour == 0
