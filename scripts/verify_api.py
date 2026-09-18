#!/usr/bin/env python3
"""只读自检: Bitget 密钥/账户/合约池/费率/资金费率/LLM 端点。不发起任何交易。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.ai import build_provider  # noqa: E402
from supermarket.bitget_client import BitgetClient  # noqa: E402
from supermarket.config import Config  # noqa: E402


def main() -> None:
    cfg = Config.load("config.yaml")
    print("=" * 60)
    print("bitget-supermarket-trader 只读自检")
    print("=" * 60)

    # 1. 密钥
    print(f"[{'OK' if cfg.bitget.ready else 'FAIL'}] Bitget 密钥: "
          f"{'已配置' if cfg.bitget.ready else '缺失(填 .env)'}")
    if not cfg.bitget.ready:
        sys.exit(1)

    bg = BitgetClient(cfg.bitget.base_url, cfg.bitget.api_key,
                      cfg.bitget.secret, cfg.bitget.passphrase)

    # 2. 账户
    try:
        acc = bg.account("NVDAUSDT")
        eq = float(acc.get("usdtEquity", 0) or 0)
        avail = float(acc.get("available", 0) or 0)
        print(f"[OK] 账户: 权益 ${eq:.4f} 可用 ${avail:.4f}")
        if eq <= 0:
            print("     ⚠️  余额为 0: 实盘前需充值(建议 $20-50 起始)")
    except Exception as e:
        print(f"[FAIL] 账户查询: {str(e)[:100]}")
        sys.exit(1)

    # 3. 合约池
    try:
        sc = bg.stock_contracts()
        print(f"[OK] 美股永续池: {len(sc)} 个")
        ticks = {t["symbol"]: t for t in bg.tickers()}
        for s in cfg.hot_symbols[:8]:
            t = ticks.get(s)
            if t:
                vol = float(t.get("usdtVolume", 0) or 0)
                print(f"     {s}: 24h成交 ${vol/1e6:.1f}M "
                      f"24h {float(t.get('changeUtc24h',0) or 0)*100:+.2f}% "
                      f"费率 {float(t.get('fundingRate',0) or 0)*100:+.4f}%")
    except Exception as e:
        print(f"[FAIL] 合约池: {str(e)[:100]}")

    # 4. LLM
    prov = build_provider(cfg)
    print(f"[{'OK' if prov.name != 'fallback-hold' else 'WARN'}] LLM: {prov.name}"
          + ("" if prov.name != "fallback-hold" else " (未配置→自动HOLD, 决策不开仓)"))

    # 5. 残留计划单提醒
    try:
        plans = bg.v3_strategy_orders()
        if plans:
            print(f"⚠️  未触发计划单 {len(plans)} 个(旧残留): {[p.get('symbol') for p in plans[:5]]}")
            print("    实盘前建议人工在 App 里确认/清理")
    except Exception:
        pass

    print("=" * 60)
    print("自检完成。实盘解锁条件: 余额>0 + LIVE_CONFIRM=yes")


if __name__ == "__main__":
    main()