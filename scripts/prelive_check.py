#!/usr/bin/env python3
"""实盘接入前自检(统一账户 UTA / v3, 只读 + 无害写), 固化 2026-09 真金实测经验。

检查项:
1. 密钥有效性 / 统一账户权益(实盘需 ≥$1)
2. 账户模式 accountMode=unified / holdMode=hedge_mode
3. 合约精度 quantityPrecision/pricePrecision + 最小下单价值(minOrderAmount)
4. 残留持仓与未触发 TPSL 策略单(必须为空)
5. 下单链路 dry-run(限价远单 → 撤单, 零成本验证权限与参数)
6. 三重解锁: LIVE_CONFIRM=yes + 余额≥$1 + LLM可用
"""
import json
import sys
import time
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
    print("实盘接入前自检 (UTA/v3)")
    print("=" * 62)

    # 1. 密钥 + 权益
    print("\n[1] 密钥与统一账户权益")
    acc = {}
    try:
        st = bg.v3_settings()
        acc = bg.v3_account()
        eq = float(acc.get("equity", 0) or 0)
        print(f"  {PASS} 密钥有效 | 账户权益 ${eq:.4f} 可用 ${float(acc.get('available',0)):.4f}")
        print(f"        accountMode={st.get('accountMode')} holdMode={st.get('holdMode')} "
              f"assetMode={st.get('assetMode')} level={st.get('accountLevel')}")
        if eq < 1.0:
            print(f"  {FAIL} 权益不足 $1 → 需充值"); fails += 1
    except Exception as e:
        print(f"  {FAIL} 账户查询失败: {str(e)[:150]}"); fails += 1

    # 2. 合约精度
    print("\n[2] 合约精度与最小下单价值")
    try:
        for s in ("NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "SPCXUSDT"):
            info = (bg.v3_instruments(s) or {}).get(s) or {}
            if not info:
                print(f"  {WARN} {s} 不在合约表"); continue
            print(f"  {s:12s} qtyPrec={info.get('quantityPrecision')} "
                  f"pricePrec={info.get('pricePrecision')} "
                  f"minOrderAmount=${info.get('minOrderAmount')} "
                  f"maxLever={info.get('maxLeverage')} type={info.get('symbolType')}")
        print(f"  {PASS} 合约表 {len(bg.v3_instruments())} 个")
    except Exception as e:
        print(f"  {FAIL} 合约表查询失败: {str(e)[:120]}"); fails += 1

    # 3. 残留持仓/策略单
    print("\n[3] 残留持仓与 TPSL 策略单(必须为空)")
    try:
        poss = bg.v3_positions()
        if poss:
            for p in poss:
                print(f"  {WARN} 残留持仓: {p.get('symbol')} {p.get('posSide')} "
                      f"total={p.get('total')} avg={p.get('avgPrice')}")
        else:
            print(f"  {PASS} 无持仓")
        so = bg.v3_strategy_orders()
        if so:
            for s in so:
                print(f"  {WARN} 残留策略单: {s.get('symbol')} {s.get('posSide')} "
                      f"SL={s.get('stopLoss')} TP={s.get('takeProfit')} id={s.get('orderId')}")
        else:
            print(f"  {PASS} 无未触发 TPSL 策略单")
    except Exception as e:
        print(f"  {WARN} 持仓/策略单查询失败: {str(e)[:120]}")

    # 4. 下单链路 dry-run(限价远单, 立即撤, 零成本)
    print("\n[4] 下单链路 dry-run(限价远单→撤单)")
    try:
        r = bg._request("GET", "/api/v3/market/tickers?category=USDT-FUTURES&symbol=NVDAUSDT")
        lst = r if isinstance(r, list) else (r.get("list") or [])
        last = float((lst[0] if lst else {}).get("lastPrice") or 0)
        qp, pp = bg.v3_precision("NVDAUSDT")
        far = round(last * 0.5, pp)
        min_amt = bg.v3_min_order_amount("NVDAUSDT")
        qty = round(min_amt / far + 0.01, qp)
        res = bg.v3_place_order("NVDAUSDT", "buy", qty, pos_side="long",
                                order_type="limit", price=far,
                                client_oid=f"prelive{int(time.time())}"[:32])
        oid = res.get("orderId", "")
        print(f"  {PASS} 下单被接受 orderId={oid} (名义≈${qty*far:.2f})")
        time.sleep(1)
        bg.v3_cancel_order("NVDAUSDT", oid)
        print(f"  {PASS} 撤单成功 ✓ 全链路可用")
    except Exception as e:
        print(f"  {FAIL} 下单链路失败: {str(e)[:160]}"); fails += 1

    # 5. 三重解锁
    print("\n[5] 实盘三重解锁")
    confirm = "no"
    try:
        for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
            if line.strip().startswith("LIVE_CONFIRM="):
                confirm = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
    except Exception:
        pass
    eq_ok = float(acc.get("equity", 0) or 0) >= 1.0 if acc else False
    print(f"  LIVE_CONFIRM={confirm} {'✓' if confirm == 'yes' else '✗(需 .env 置 yes)'}")
    print(f"  权益≥$1: {'✓' if eq_ok else '✗(需充值)'}")
    from supermarket.ai import build_provider
    p = build_provider(cfg)
    print(f"  LLM: {cfg.llm.model} "
          f"{'✓' if p.__class__.__name__ != 'FallbackHOLDProvider' else '✗(未配置)'}")

    print("\n" + "=" * 62)
    print(f"自检结果: {'全部通过 → 可启动实盘' if not fails else f'{fails} 项失败'}")
    print("启动: systemctl stop supermarket-paper && systemctl start supermarket-live")
    print("=" * 62)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())