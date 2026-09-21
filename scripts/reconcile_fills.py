#!/usr/bin/env python
"""交易所 fills 权威对账(2026-09-21 重大账务修复后新增)。

以 v3 /trade/fills 的已实现盈亏为**唯一权威**,审计 ai_memory 的 closed 记录:
  1. 拉取最近 N 天全量 fills(注意: v3 fills 的 symbol 参数被服务器忽略, 返回全量)
  2. 提取所有 close* 成交, 按 (symbol, createdTime 秒) 聚合(交易所分批拆单合并为同一笔平仓)
  3. 与 ai_memory closed 逐标的核对笔数与 pnl 合计
  4. 输出差异清单; 完全一致 → 0, 有差异 → 1(便于日报/定时任务捕获)

用法: python scripts/reconcile_fills.py [--days 14] [--min-pnl 0.001]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MIN_PNL_DEFAULT = 0.001   # 忽略 ≤0.001 的尘埃成交(试运行单等)


def scan_fills(bg, days: int = 14, since_ms: int = 0) -> list[dict]:
    """拉取近 days 天全量成交, 返回 close 台账(同秒同标的合并)。
    since_ms: 只统计该时刻之后的成交(2026-09-21: 排除超市启动前 9/8-9/11 的历史试运行)。"""
    end = int(time.time() * 1000)
    start = int((time.time() - days * 86400) * 1000)
    r = bg._request("GET", f"/api/v3/trade/fills?startTime={start}&endTime={end}")
    lst = (r.get("list") or []) if isinstance(r, dict) else r
    buckets: dict[tuple[str, int], dict] = {}
    for f in lst or []:
        if not str(f.get("tradeSide", "")).startswith("close"):
            continue
        ts = int(f.get("createdTime", 0) or 0)
        if since_ms and ts < since_ms:
            continue
        sym = str(f.get("symbol", "")).upper()
        key = (sym, ts // 1000)          # 同秒 = 同一批平仓的拆单
        b = buckets.setdefault(key, {"symbol": sym, "ts": ts, "qty": 0.0,
                                     "pnl": 0.0, "price": 0.0, "n": 0})
        b["qty"] += float(f.get("execQty", 0) or 0)
        b["pnl"] += float(f.get("execPnl", 0) or 0)
        b["price"] = float(f.get("execPrice", 0) or 0)
        b["n"] += 1
    return [buckets[k] for k in sorted(buckets)]


def scan_memory(live_dir: Path) -> list[dict]:
    """ai_memory 的 closed 记录。"""
    try:
        d = json.loads((live_dir / "ai_memory.json").read_text())
    except Exception:
        return []
    return [r for r in (d.get("decisions") or []) if r.get("outcome") == "closed"]


def audit(bg, cfg, live_dir: Path, days: int = 14,
          min_pnl: float = MIN_PNL_DEFAULT) -> dict:
    """返回 {ok, fill_total, mem_total, n_closes, diffs:[...], lines:[...]}。
    起始基准 = ai_memory 最早记录往前 1 天(排除超市启动前的历史试运行成交)。"""
    mem = scan_memory(live_dir)
    since = 0.0
    if mem:
        since = min(float(r.get("ts", 0) or 0) for r in mem) - 86400
    fills = [f for f in scan_fills(bg, days, since_ms=int(max(0.0, since) * 1000))
             if abs(f["pnl"]) > min_pnl]
    fill_by_sym: dict[str, list[dict]] = {}
    for f in fills:
        fill_by_sym.setdefault(f["symbol"], []).append(f)
    mem_by_sym: dict[str, list[dict]] = {}
    for m in mem:
        mem_by_sym.setdefault(m["symbol"], []).append(m)

    fill_total = sum(f["pnl"] for f in fills)
    mem_total = sum(float(m.get("pnl", 0) or 0) for m in mem)
    symbols = sorted(set(fill_by_sym) | set(mem_by_sym))
    diffs: list[dict] = []
    for s in symbols:
        fp = sum(f["pnl"] for f in fill_by_sym.get(s, []))
        mp = sum(float(m.get("pnl", 0) or 0) for m in mem_by_sym.get(s, []))
        nf, nm = len(fill_by_sym.get(s, [])), len(mem_by_sym.get(s, []))
        if abs(fp - mp) > 1e-6 or (nf != nm and abs(fp - mp) > 1e-6):
            diffs.append({"symbol": s, "fill_pnl": fp, "mem_pnl": mp,
                          "n_fills": nf, "n_mem": nm,
                          "note": _note(nf, nm, fp, mp)})
    lines = [
        f"fills({len(fills)}批, {len(symbols)}标的) 合计 ${fill_total:+.4f} "
        f"vs 账本({len(mem)}条) ${mem_total:+.4f}",
    ]
    for d in diffs:
        lines.append(f"  ⚠️ {d['symbol']}: fills({d['n_fills']}批 ${d['fill_pnl']:+.4f}) "
                     f"vs 账本({d['n_mem']}条 ${d['mem_pnl']:+.4f}) {d['note']}")
    return {"ok": not diffs, "fill_total": fill_total, "mem_total": mem_total,
            "n_closes": len(mem), "diffs": diffs, "lines": lines}


def _note(nf: int, nm: int, fp: float, mp: float) -> str:
    if nf == 0:
        return "❌ 账本有记录但 fills 无此成交(疑为误补录)"
    if nm == 0:
        return "❌ fills 有平仓但账本漏记"
    if nf != nm:
        return f"❌ 批数不符(fills {nf} vs 账本 {nm})"
    return "❌ pnl 值不一致"


def main() -> int:
    import argparse
    from supermarket.bitget_client import BitgetClient
    from supermarket.config import Config

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-pnl", type=float, default=MIN_PNL_DEFAULT)
    args = ap.parse_args()
    cfg = Config.load()
    bg = BitgetClient(**cfg.bitget.__dict__)
    r = audit(bg, cfg, ROOT / "state" / "live", days=args.days,
              min_pnl=args.min_pnl)
    print("🔍 账目审计(fills 权威):")
    print("  " + r["lines"][0])
    for d in r["diffs"]:
        print("   " + d["symbol"] + " " + d["note"])
    if r["ok"]:
        print("✅ 完全一致")
        return 0
    print("⚠️ 存在差异 — 请人工核对或按 fills 修正 ai_memory")
    return 1


if __name__ == "__main__":
    sys.exit(main())