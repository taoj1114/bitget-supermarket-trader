#!/usr/bin/env python3
"""实盘接入前自检(只读 + 少量无害写操作), 固化 2026-09 实战经验。

检查项:
1. 密钥有效性 / 合约账户余额(实盘必须 > 0)
2. 仓位模式 posMode(必须 hedge_mode → posSide/holdSide 语义)
3. 全仓交叉杠杆设置
4. 合约精度 volumePlace / 最小名义 minTradeUSDT
5. 现有持仓与未触发计划单(必须为空, 否则有残留)
6. TPSL 前置条件(无持仓挂 TPSL 应返回 43023——证明 order flow 正确)
7. 三重解锁状态: LIVE_CONFIRM=yes + 余额>0 + LLM可用
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.bitget_client import BitgetClient
from supermarket.config import Config

PASS, FAIL, WARN = "[OK]", "[FAIL]", "[WARN]"


def main() -> int:
    cfg = Config.load()
    bg = BitgetClient(**cfg.bitget.__dict__)
    fails = 0

    print("=" * 62)
    print("实盘接入前自检 (prelive check)")
    print("=" * 62)

    # 1. 密钥 + 余额
    print("\n[1] 密钥与余额")
    sym = "NVDAUSDT"
    acc = {}
    try:
        acc = bg.account(sym)
        eq = float(acc.get("usdtEquity", 0) or 0)
        avail = float(acc.get("available", 0) or 0)
        pm = acc.get("posMode", "?")
        lev = acc.get("crossedMarginLeverage", "?")
        print(f"  {PASS} 密钥有效 | 权益 ${eq:.4f} 可用 ${avail:.4f}")
        if eq <= 0:
            print(f"  {FAIL} 账户余额为 0 → 实盘无法开仓(需充值 $20~50)")
            fails += 1
        # 2. 仓位模式
        print("\n[2] 仓位模式")
        if pm == "hedge_mode":
            print(f"  {PASS} posMode={pm} → 下单须带 posSide=long/short, 杠杆 holdSide=long/short")
        else:
            print(f"  {WARN} posMode={pm}(非 hedge_mode) → 下单参数需相应调整!")
        print(f"      账户全仓杠杆设置 crossedMarginLeverage={lev}(每笔订单另带 leverage)")
    except Exception as e:
        print(f"  {FAIL} 账户查询失败: {str(e)[:150]}")
        fails += 1

    # 3. 合约精度与最小名义
    print("\n[3] 合约精度/最小名义(平仓精度不符会被拒单)")
    try:
        cs = {c["symbol"]: c for c in bg.stock_contracts()}
        n_bad = 0
        for s in ("NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "SPCXUSDT", "MSFTUSDT"):
            c = cs.get(s)
            if not c:
                print(f"  {WARN} {s} 不在美股池")
                continue
            print(f"  {s:12s} volumePlace={c.get('volumePlace')} "
                  f"sizeMultiplier={c.get('sizeMultiplier')} "
                  f"minTradeUSDT=${c.get('minTradeUSDT')} maxLever={c.get('maxLever')}")
            if int(c.get("volumePlace", -1)) < 0:
                n_bad += 1
        if n_bad:
            print(f"  {FAIL} {n_bad} 个合约精度异常")
            fails += 1
        print(f"  {PASS} 美股池 {len(cs)} 个合约")
    except Exception as e:
        print(f"  {FAIL} 合约查询失败: {str(e)[:150]}")
        fails += 1

    # 4. 残留持仓/计划单
    print("\n[4] 残留持仓与计划单(必须为空)")
    try:
        poss = bg.positions()
        active = [p for p in poss if float(p.get("holdVol", 0) or 0) > 0]
        if active:
            for p in active:
                print(f"  {WARN} 残留持仓: {p.get('symbol')} {p.get('holdSide')} "
                      f"vol={p.get('holdVol')} entry={p.get('avgEntryPrice')}")
        else:
            print(f"  {PASS} 无持仓")
        plans = bg.pending_plans() or []
        tpsl_plans = [p for p in plans if p.get("planType") in ("pos_loss", "pos_profit")]
        if tpsl_plans:
            for p in tpsl_plans:
                print(f"  {WARN} 残留计划单: {p.get('symbol')} {p.get('planType')} "
                      f"trigger={p.get('triggerPrice')} id={p.get('orderId')}")
            print(f"  {WARN} → 实盘前请先在 App 或 API 清理这些残留计划单")
        else:
            print(f"  {PASS} 无未触发 TPSL 计划单")
    except Exception as e:
        print(f"  {WARN} 持仓/计划单查询失败: {str(e)[:120]}")

    # 5. TPSL 前置条件(应返回 43023 = order flow 理解正确)
    print("\n[5] TPSL 前置条件(Bitget 要求先有仓位)")
    try:
        bg.place_tpsl("NVDAUSDT", "pos_loss", "1.0", hold_side="long")
        print(f"  {WARN} 无持仓竟然挂单成功?! 请人工核对(可能语义变化)")
    except Exception as e:
        msg = str(e)
        if "43023" in msg or "仓位不足" in msg:
            print(f"  {PASS} 无持仓挂 TPSL 被拒(43023) → 执行顺序必须是 开仓→挂TPSL")
        else:
            print(f"  {WARN} 挂 TPSL 返回非预期错误: {msg[:120]}")

    # 6. 三重解锁
    print("\n[6] 实盘三重解锁")
    env_path = Path(__file__).resolve().parent.parent / ".env"
    confirm = "no"
    try:
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("LIVE_CONFIRM="):
                confirm = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
    except Exception:
        pass
    eq_ok = float(acc.get("usdtEquity", 0) or 0) >= 1.0 if acc else False
    print(f"  LIVE_CONFIRM={'yes' if confirm == 'yes' else confirm} "
          f"{'✓' if confirm == 'yes' else '✗(需 .env 置 yes)'}")
    print(f"  余额≥$1: {'✓' if eq_ok else '✗(当前尘埃值 %.8f, 需充值)' % float(acc.get('usdtEquity', 0) or 0)}")
    try:
        from supermarket.ai import build_provider
        p = build_provider(cfg)
        print(f"  LLM: {cfg.llm.model} "
              f"{'✓' if p.__class__.__name__ != 'FallbackHOLDProvider' else '✗(未配置)'}")
    except Exception as e:
        print(f"  LLM 检查异常: {str(e)[:100]}")

    print("\n" + "=" * 62)
    if fails:
        print(f"自检结果: {fails} 项需要处理(见上方 FAIL)")
    else:
        print("自检结果: 全部通过 → 可进入最小验证单环节")
    print("实盘启动: 1) .env 设 LIVE_CONFIRM=yes  2) systemctl start supermarket-live")
    print("=" * 62)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())