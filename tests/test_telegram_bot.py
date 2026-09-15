"""Telegram 指令交互测试(假 engine, 不触网)。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.telegram_bot import HELP, TelegramCommander


class FakePos:
    symbol = "TSLAUSDT"
    direction = "long"
    avg_entry = 362.25
    qty = 0.11
    sl = 353.0
    tp = 376.6
    batches = 1
    leverage = 20


class FakeEngine:
    def __init__(self, tmp: Path):
        self.state_dir = tmp
        self.bg = self

    def _account(self):
        return {"equity": 39.5, "available": 30.0, "notional": 39.8}

    class executor:
        @staticmethod
        def positions():
            return [FakePos()]

    class risk:
        @staticmethod
        def snapshot():
            return {"day_pnl": -0.12, "consecutive_losses": 1}

    class memory:
        @staticmethod
        def closed_decisions():
            return [{"symbol": "SPCXUSDT", "pnl": 0.29, "close_reason": "AI_CLOSE"},
                    {"symbol": "TSLAUSDT", "pnl": -0.39, "close_reason": "SL_EXCHANGE"}]

    def quote(self, symbol):
        return {"lastPr": "360.00"}


def make():
    td = tempfile.TemporaryDirectory()
    return TelegramCommander(FakeEngine(Path(td.name))), td


def test_help_and_start():
    cm, _td = make()
    for t in ("/help", "/start", ""):
        out = cm.handle(t)
        assert "超市" in out and "/status" in out


def test_status_shows_equity_positions_and_pnl():
    cm, _td = make()
    out = cm.handle("/status")
    assert "39.50" in out and "TSLAUSDT" in out
    assert "SL353.00" in out.replace(" ", "") or "353" in out
    assert "今日盈亏" in out


def test_positions_detail():
    cm, _td = make()
    out = cm.handle("/positions")
    assert "TSLAUSDT" in out and "批次 1/3" in out and "20x" in out


def test_pnl_summary():
    cm, _td = make()
    out = cm.handle("/pnl")
    assert "共 2 笔" in out and "胜 1" in out
    assert "+0.29" in out or "0.2900" in out


def test_pause_resume_creates_and_removes_flag():
    cm, _td = make()
    assert not (cm.engine.state_dir / "pause.flag").exists()
    out = cm.handle("/pause")
    assert "暂停" in out
    assert (cm.engine.state_dir / "pause.flag").exists()
    out = cm.handle("/resume")
    assert "恢复" in out
    assert not (cm.engine.state_dir / "pause.flag").exists()


def test_unknown_command_returns_help():
    cm, _td = make()
    out = cm.handle("/foobar")
    assert "未知指令" in out and "/help" in out
