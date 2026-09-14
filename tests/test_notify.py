"""Telegram 告警推送测试(不触网: 未配置时静默; 配置时用假 URL 拦截)。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket import notify


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.enabled() is False
    assert notify.send("hello") is False          # 静默, 不抛异常
    notify.alert("CLOSE_FAIL", "NVDAUSDT", "boom")  # 不应抛


def test_enabled_requires_both(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.enabled() is False
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    assert notify.enabled() is True


def test_send_hits_api_and_handles_failure(monkeypatch):
    """配置齐全时调用 Telegram API; 网络异常被吞掉(不影响交易主流程)。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    calls = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify._last_sent.clear()
    assert notify.send("test message") is True
    assert calls and "sendMessage" in calls[0]

    # 网络异常 → 返回 False 不抛
    def boom(req, timeout=10):
        raise OSError("network down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    notify._last_sent.clear()
    assert notify.send("again") is False


def test_dedup_window(monkeypatch):
    """同 key 在窗口内只发一次(防告警轰炸)。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    sent = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=10: (sent.append(1), FakeResp())[1])
    notify._last_sent.clear()
    assert notify.send("a", dedup_key="k1", dedup_window_s=600) is True
    assert notify.send("b", dedup_key="k1", dedup_window_s=600) is False   # 窗口内被去重
    assert len(sent) == 1


def test_trade_message_format():
    text_ok = True
    try:
        # 未配置 → 内部静默; 这里只验证构造函数不抛异常
        notify.trade_open("NVDAUSDT", "long", 212.5, 0.03, 200.0, 225.0, batches=2)
        notify.trade_close("NVDAUSDT", "long", 0.42, "AI_CLOSE")
        notify.risk_event("熔断: 连续3次亏损")
        notify.status("净值 $39.5")
    except Exception:
        text_ok = False
    assert text_ok
