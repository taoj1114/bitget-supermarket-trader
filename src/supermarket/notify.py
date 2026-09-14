"""Telegram 告警推送(.env 配 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID 即启用)。

设计原则:
- 未配置 → 完全静默(不影响实盘交易)
- 发送失败 → 只记日志, 绝不抛异常打断交易主流程
- 只在关键事件推送: 开仓/平仓成交、风控熔断、裸仓/平仓失败等异常
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}
_MIN_INTERVAL_S = 3.0          # 简单节流(Telegram 限制 ~30 msg/s, 保守)


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, dedup_key: str = "", dedup_window_s: float = 300.0) -> bool:
    """发送消息。dedup_key 非空时, 同 key 在窗口内只发一次(防重复轰炸)。"""
    if not enabled():
        return False
    if dedup_key:
        now = time.time()
        if now - _last_sent.get(dedup_key, 0) < dedup_window_s:
            return False
        _last_sent[dedup_key] = now
    now = time.time()
    if now - _last_sent.get("__global__", 0) < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - (now - _last_sent["__global__"]))
    _last_sent["__global__"] = time.time()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text[:3900],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
        if not ok:
            log.warning("telegram 推送未确认: %s", text[:60])
        return bool(ok)
    except Exception as e:
        log.warning("telegram 推送失败(不影响交易): %s", str(e)[:120])
        return False


# ---------- 业务事件封装 ----------

def alert(kind: str, symbol: str, detail: str) -> None:
    """关键异常告警(裸仓/平仓失败/补挂失败等)。"""
    emoji = {"CLOSE_FAIL": "🚨", "PROTECTION_REPAIRED": "🔧",
             "PROTECTION_REPAIR_FAIL": "🚨", "OPEN_FAIL": "⚠️"}.get(kind, "⚠️")
    send(f"{emoji} <b>[{kind}]</b> {symbol}\n{detail}",
         dedup_key=f"{kind}:{symbol}", dedup_window_s=600)


def trade_open(symbol: str, direction: str, price: float, qty: float,
               sl: float, tp: float, batches: int = 1) -> None:
    side = "开多" if direction == "long" else "开空"
    send(f"🟢 <b>{side}</b> {symbol}\n价 ${price:.2f} 量 {qty} "
         f"SL ${sl:.2f} TP ${tp:.2f}\n批次 {batches}/3")


def trade_close(symbol: str, direction: str, pnl: float, reason: str) -> None:
    emoji = "💰" if pnl >= 0 else "🔻"
    send(f"{emoji} <b>平仓</b> {symbol} ({direction})\n"
         f"真实盈亏 <b>${pnl:+.4f}</b>\n原因: {reason}")


def risk_event(detail: str) -> None:
    send(f"⛔ <b>风控事件</b>\n{detail}", dedup_key=f"risk:{detail[:40]}",
         dedup_window_s=1800)


def status(text: str) -> None:
    send(f"📊 {text}")