#!/usr/bin/env python
"""超市每日体检 → Telegram 推送(2026-09)。

用途: 观察期风险监控 —— 及时发现"门控过严导致零开仓"或异常。
由 systemd timer 每日 09:00(北京) 触发, 也可手动运行。
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daily_report")

CST = timezone(timedelta(hours=8))
LIVE = ROOT / "state" / "live"


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main() -> None:
    from supermarket import notify
    from supermarket.bitget_client import BitgetClient
    from supermarket.config import Config

    now = datetime.now(CST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    yday_start = today_start - 86400

    cfg = Config.load()
    lines = [f"📊 <b>超市日报</b> {now.strftime('%m-%d %H:%M')}"]

    # 1) 账户与持仓(实测)
    equity = day_pnl = 0.0
    n_pos = 0
    try:
        bg = BitgetClient(**cfg.bitget.__dict__)
        acc = bg.v3_account()
        equity = float(acc.get("equity", 0))
        poses = bg.v3_positions()
        n_pos = len(poses)
        lines.append(f"权益 <b>${equity:.2f}</b> | 持仓 {n_pos} 个")
        for p in poses[:5]:
            try:
                sym = p["symbol"]
                side = p["posSide"]
                q = bg.quote(sym)
                last = float(q.get("lastPr", 0) or 0)
                avg = float(p.get("avgPrice", 0) or 0)
                if side == "long":
                    pct = (last / avg - 1) * 100
                else:
                    pct = (avg / last - 1) * 100
                lines.append(f"  · {sym} {'多' if side == 'long' else '空'} {avg:.2f}→{last:.2f} ({pct:+.2f}%)")
            except Exception:
                continue
    except Exception as e:
        lines.append(f"⚠️ 账户查询失败: {str(e)[:50]}")

    # 2) 当日成交与盈亏(以 ai_memory 逐笔记录为权威; orders 仅作辅助)
    mem0 = load(LIVE / "ai_memory.json", {})
    decs0 = mem0.get("decisions") or []
    opens = [d for d in decs0 if float(d.get("ts", 0) or 0) >= today_start]
    closes = [d for d in decs0 if d.get("outcome") == "closed"
              and float(d.get("close_ts", 0) or 0) >= today_start]
    day_pnl = sum(float(d.get("pnl", 0) or 0) for d in closes)
    lines.append(f"今日: 开仓 {len(opens)} | 平仓 {len(closes)} | 净盈亏 <b>${day_pnl:+.4f}</b>")

    # 3) 风控拦截统计(观察门控是否过严)
    mem = load(LIVE / "ai_memory.json", {})
    holds = [h for h in (mem.get("holds") or []) if h.get("ts", 0) >= today_start]
    kinds = {"大盘门控": 0, "中段下跌禁区": 0, "止损过近/参数": 0, "当日回撤熔断": 0,
             "连亏熔断": 0, "其他拒绝": 0, "AI主动HOLD": 0}
    for h in holds:
        r = str(h.get("reason", ""))
        if not r.startswith("REJECT"):
            kinds["AI主动HOLD"] += 1
        elif "大盘日线向下" in r:
            kinds["大盘门控"] += 1
        elif "中段下跌禁区" in r:
            kinds["中段下跌禁区"] += 1
        elif "熔断" in r:
            kinds["连亏熔断"] += 1
        elif "回撤" in r:
            kinds["当日回撤熔断"] += 1
        elif "止损" in r or "止盈" in r or "盈亏比" in r:
            kinds["止损过近/参数"] += 1
        else:
            kinds["其他拒绝"] += 1
    active = " | ".join(f"{k} {v}" for k, v in kinds.items() if v)
    lines.append(f"风控: {active or '无记录'}")

    # 3.5) 策略业绩(逐笔交易记录累加, **不受外部注资影响**) + 注资核对
    closed = [d for d in (mem.get("decisions") or []) if d.get("outcome") == "closed"]
    realized = sum(float(d.get("pnl", 0) or 0) for d in closed)
    wins = len([d for d in closed if float(d.get("pnl", 0) or 0) > 0])
    holds_h = []
    for d in closed:
        o, c2 = float(d.get("ts", 0) or 0), float(d.get("close_ts", 0) or 0)
        if o and c2:
            holds_h.append((c2 - o) / 3600)
    cap = load(LIVE / "capital.json", {})
    dep = sum(float(d.get("amount") or 0) for d in (cap.get("deposits") or []))
    avg_hold = f"{statistics.median(holds_h)*60:.0f}分" if holds_h and statistics.median(holds_h) < 1 else (f"{statistics.median(holds_h):.1f}时" if holds_h else "—")
    lines.append(f"📈 策略业绩(累计, 不含注资): 已实现 <b>${realized:+.4f}</b> | "
                 f"{len(closed)}笔 {wins}胜{len(closed)-wins}负 | 持有中位 {avg_hold}")
    if dep:
        lines.append(f"💰 外部注资合计 ${dep:.2f} → 账户权益 = 注资 + 基线 + 策略业绩")

    # 3.6) 账目审计(v3 fills 唯一权威; 2026-09-21 重大账务修复后每日自动核账)
    try:
        from reconcile_fills import audit as audit_ledger
        r = audit_ledger(bg, cfg, LIVE, days=14)
        if r["ok"]:
            lines.append(f"🔍 账目审计: 与交易所 fills 一致 ✓ ({r['n_closes']}笔已平 ${r['fill_total']:+.4f})")
        else:
            lines.append(f"🔍 账目审计: ⚠️ 差异 {len(r['diffs'])} 条 — fills ${r['fill_total']:+.4f} vs 账本 ${r['mem_total']:+.4f}")
            for dd in r["diffs"][:3]:
                lines.append(f"   · {dd['symbol']}: {dd['note']}")
    except Exception as e:
        lines.append(f"🔍 账目审计失败: {str(e)[:60]}  (不影响其他项目)")

    # 4) 熔断状态 + 告警
    br = load(LIVE / "breakers.json", {})
    lines.append(f"连亏 {br.get('consecutive_losses', 0)} 次 | 当日已实现 ${float(br.get('day_pnl', 0)):+.4f}")
    alerts = load(LIVE / "real.json", {}).get("alerts") or []
    new_alerts = [a for a in alerts if float(a.get("ts", 0) or 0) >= today_start]
    lines.append(f"告警: {len(new_alerts)} 条" + (f" ⚠️ 最新: {str(new_alerts[-1])[:60]}" if new_alerts else ""))

    # 5) 观察期提示(长期0开仓 = 需要检查门控)
    if len(opens) == 0 and n_pos == 0:
        lines.append("ℹ️ 今日无开仓且空仓 —— 若连续 3 天如此, 需检查门控是否过严")

    text = "\n".join(lines)
    print(text)
    ok = notify.send(text)
    print(f"\nTelegram 推送: {'✅ 成功' if ok else '❌ 未发送(检查 .env 凭证)'}")


if __name__ == "__main__":
    main()
