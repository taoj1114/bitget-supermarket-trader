"""超市系统 Telegram 指令交互(接管 bot, 替代已删除的 DSA)。

轮询 getUpdates 处理用户指令, 回复实时状态:
  /status   权益+持仓+浮盈总览
  /positions 持仓明细
  /pnl      已实现盈亏汇总(近期平仓)
  /pause    暂停开新仓(管仓/止损照常)
  /resume   恢复开仓
  /help     帮助

设计: 独立守护线程, 异常不致命; 与 notify 共用同一个 bot(只增指令, 不影响推送)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

from supermarket import notify

log = logging.getLogger(__name__)

HELP = (
    "<b>🛒 超市实盘系统</b>\n"
    "策略: 进货四道门 / 有盈利就卖 / 结构未坏拿住 / 天气门\n\n"
    "指令:\n"
    "/status — 权益 · 持仓 · 浮盈总览\n"
    "/positions — 持仓明细\n"
    "/pnl — 已实现盈亏汇总\n"
    "/pause — 暂停开新仓(止损仍生效)\n"
    "/resume — 恢复开仓\n"
    "/help — 本帮助"
)


def install_menu() -> bool:
    """设置 bot 命令菜单(替代已删除的 DSA 旧菜单)。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    cmds = [("help", "帮助"), ("status", "权益·持仓·浮盈总览"),
            ("positions", "持仓明细"), ("pnl", "已实现盈亏汇总"),
            ("pause", "暂停开新仓"), ("resume", "恢复开仓")]
    try:
        data = urllib.parse.urlencode({
            "commands": json.dumps([{"command": c, "description": d} for c, d in cmds]),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setMyCommands", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
        log.info("Telegram 菜单安装: %s", "成功" if ok else "失败")
        return bool(ok)
    except Exception as e:
        log.warning("菜单安装失败: %s", str(e)[:100])
        return False


class TelegramCommander:
    def __init__(self, engine):
        self.engine = engine
        self.offset: int | None = None

    # ---------- Telegram API ----------
    def _token(self) -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "")

    def _chat_id(self) -> str:
        return os.environ.get("TELEGRAM_CHAT_ID", "")

    def _get_updates(self, timeout: int = 20) -> list[dict]:
        token = self._token()
        if not token:
            return []
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if self.offset is not None:
            params["offset"] = self.offset
        url = (f"https://api.telegram.org/bot{token}/getUpdates?"
               + urllib.parse.urlencode(params))
        with urllib.request.urlopen(url, timeout=timeout + 15) as r:
            d = json.loads(r.read().decode())
        return d.get("result") or []

    def reply(self, text: str) -> None:
        notify.send(text)

    # ---------- 指令处理 ----------
    def handle(self, text: str) -> str:
        cmd = (text or "").strip().split()[0].lower().lstrip("/") if text else ""
        try:
            if cmd in ("start", "help", ""):
                return HELP
            if cmd == "status":
                return self._status()
            if cmd in ("positions", "pos"):
                return self._positions()
            if cmd == "pnl":
                return self._pnl()
            if cmd == "pause":
                (self.engine.state_dir / "pause.flag").write_text(
                    f"paused at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                return "⏸ 已暂停<b>开新仓</b>(持仓管理/止损照常执行)。\n恢复用 /resume"
            if cmd == "resume":
                f = self.engine.state_dir / "pause.flag"
                if f.exists():
                    f.unlink()
                return "▶️ 已恢复开仓。"
            return f"未知指令: {cmd}\n\n" + HELP
        except Exception as e:
            log.warning("指令处理异常 %s: %s", cmd, str(e)[:100])
            return f"⚠️ 处理 {cmd} 出错: {str(e)[:100]}"

    # ---------- 查询实现 ----------
    def _status(self) -> str:
        acc = self.engine._account()
        poses = self.engine.executor.positions()
        lines = [f"<b>🛒 超市状态</b>  ({time.strftime('%m-%d %H:%M')})",
                 f"权益 <b>${float(acc.get('equity', 0)):.2f}</b> | 可用 ${float(acc.get('available', 0)):.2f}",
                 f"已用名义 ${float(acc.get('notional', 0)):.0f} (上限 ${float(acc.get('equity', 0)) * 6:.0f})",
                 f"持仓 {len(poses)} 个"]
        s_line = getattr(self.engine, "_sentiment_line", "")
        if s_line:
            lines.append("📊 " + s_line)
        if (self.engine.state_dir / "pause.flag").exists():
            lines.append("⏸ <b>已暂停开新仓</b>")
        if not poses:
            lines.append("(空仓)")
        for p in poses:
            try:
                q = self.engine.bg.quote(p.symbol)
                last = float(q.get("lastPr", 0) or 0)
                pnl = ((last - p.avg_entry) / p.avg_entry * 100) if p.direction == "long" \
                    else ((p.avg_entry - last) / p.avg_entry * 100)
                lines.append(f"· {p.symbol} {'多' if p.direction == 'long' else '空'} "
                             f"${p.avg_entry:.2f}→${last:.2f} ({pnl:+.2f}%) "
                             f"SL{p.sl:.2f} TP{p.tp:.2f} 批次{p.batches}/3")
            except Exception:
                lines.append(f"· {p.symbol} 入库价 ${p.avg_entry:.2f} 批次{p.batches}/3")
        snap = self.engine.risk.snapshot()
        lines.append(f"今日盈亏 ${float(snap.get('day_pnl', 0)):+.3f} | "
                     f"连亏 {snap.get('consecutive_losses', 0)}")
        return "\n".join(lines)

    def _positions(self) -> str:
        poses = self.engine.executor.positions()
        if not poses:
            return "空仓(超市货架空的)"
        out = ["<b>持仓明细</b>"]
        for p in poses:
            out.append(f"· <b>{p.symbol}</b> {'多' if p.direction == 'long' else '空'} "
                       f"量 {p.qty} 入库 ${p.avg_entry:.2f}\n  SL ${p.sl:.2f} / TP ${p.tp:.2f} "
                       f"| 批次 {p.batches}/3 | 杠杆 {p.leverage}x")
        return "\n".join(out)

    def _pnl(self) -> str:
        closed = self.engine.memory.closed_decisions()
        if not closed:
            return "还没有平仓记录"
        wins = [c for c in closed if float(c.get("pnl", 0)) > 0]
        total = sum(float(c.get("pnl", 0)) for c in closed)
        out = [f"<b>已实现盈亏</b>  共 {len(closed)} 笔 "
               f"(胜 {len(wins)} / 负 {len(closed) - len(wins)})",
               f"累计 <b>${total:+.4f}</b>"]
        for c in closed[-5:]:
            out.append(f"· {c.get('symbol')} ${float(c.get('pnl', 0)):+.4f} "
                       f"({c.get('close_reason', '')})")
        return "\n".join(out)

    # ---------- 轮询主循环 ----------
    def run_forever(self) -> None:
        if not notify.enabled():
            log.info("Telegram 未配置, 指令线程不启动")
            return
        log.info("Telegram 指令线程启动(轮询)")
        while True:
            try:
                updates = self._get_updates(timeout=20)
                for u in updates:
                    self.offset = int(u.get("update_id", 0)) + 1
                    msg = u.get("message") or {}
                    chat = msg.get("chat") or {}
                    if str(chat.get("id")) != str(self._chat_id()):
                        continue          # 只响应主人
                    text = msg.get("text") or ""
                    if text:
                        log.info("收到指令: %s", text[:40])
                        self.reply(self.handle(text))
            except Exception as e:
                log.debug("指令轮询异常(继续): %s", str(e)[:100])
                time.sleep(5)
