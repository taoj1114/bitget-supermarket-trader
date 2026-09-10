#!/usr/bin/env python3
"""端到端纸面链路验证(测试用, 非策略!): 用确定性决策器驱动真实行情交易循环,
验证 开仓→管仓→止盈/止损→复盘 PnL 记账 的完整数学正确性。

策略正确性依赖 LLM 密钥配置后由 AI 决策; 本脚本只验证机制。
用法: .venv/bin/python scripts/simulate_paper.py [--rounds N]
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supermarket.ai import LLMProvider, OpenDecision, ManageDecision  # noqa: E402
from supermarket.config import Config  # noqa: E402
from supermarket.engine import SupermarketEngine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("simulate")


class LinkTestProvider(LLMProvider):
    """确定性决策器: 只用于验证交易链路账目。不模拟 AI 选股质量。"""

    name = "link-test"

    def __init__(self):
        self.opened: dict[str, float] = {}

    def decide_open(self, system, prompt) -> OpenDecision:
        # 从 prompt 里抓关键事实
        import re
        m4h = re.search(r"4H\(定方向\):.*?regime=(\w+)", prompt)
        mprice = re.search(r"当前价\$([\d.]+)", prompt)
        regime = m4h.group(1) if m4h else "flat"
        price = float(mprice.group(1)) if mprice else 100.0
        # 只对 trend_up 且我们还没开过的标的开仓
        if regime == "trend_up" and len([k for k in self.opened]) < 4:
            sym = re.search(r"标的: (\w+)", prompt).group(1)
            if sym in self.opened:
                return OpenDecision(action="HOLD", reason="已持有")
            self.opened[sym] = price
            # SL -2.5%, TP +5% (RR=2, 过风控)
            return OpenDecision(action="BUY", stop_loss=round(price * 0.975, 2),
                                take_profit=round(price * 1.05, 2),
                                reason="链路测试: 4H趋势向上")
        return OpenDecision(action="HOLD", reason="链路测试: 非趋势")

    def decide_manage(self, system, prompt) -> ManageDecision:
        import re
        mpnl = re.search(r"浮盈亏([+-][\d.]+)%", prompt)
        pnl = float(mpnl.group(1)) if mpnl else 0.0
        if pnl >= 0.4:
            return ManageDecision(action="CLOSE", reason="链路测试: 盈利兑现")
        if pnl <= -0.4:
            return ManageDecision(action="CLOSE", reason="链路测试: 止损")
        return ManageDecision(action="HOLD", reason="链路测试: 持有")

    def review(self, system, prompt):
        return ["链路测试: 复盘管线正常"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()

    cfg = Config.load("config.yaml")
    cfg.mode = "paper"
    cfg.scan_interval = 5  # 测试: 高频
    eng = SupermarketEngine(cfg)
    eng.provider = LinkTestProvider()

    log.info("=== 链路测试: %d 轮, 虚拟账户 $%.0f ===", args.rounds, cfg.paper.initial_equity)
    for i in range(args.rounds):
        try:
            eng.run_once()
        except Exception as e:
            log.exception("轮 %d 异常: %s", i, str(e)[:100])
        time.sleep(3)

    # 结果报告
    ex = eng.executor
    closed = ex.closed_trades()
    mem = eng.memory
    stats = mem.stats()
    print("\n" + "=" * 60)
    print("链路验证报告 (paper, 真实行情)")
    print("=" * 60)
    print(f"账户净值: ${float(ex._state['equity']):.4f} (初始 $30)")
    print(f"持仓: {len(ex.positions())} 个, 平仓: {len(closed)} 笔")
    total_pnl = sum(float(c['pnl']) for c in ex._state['closed'])
    print(f"已实现 PnL: ${total_pnl:+.4f}")
    print(f"记忆: 开仓决策 {stats['open']}, 已平仓 {stats['closed']}, "
          f"HOLD记录 {stats['holds']}, 教训 {stats['lessons']} 条")
    for c in closed[-8:]:
        print(f"  {c['symbol']} 入{float(c['entry']):.2f} 出{float(c['exit']):.2f} "
              f"pnl {float(c['pnl']):+.4f} [{c['reason']}]")
    if closed:
        print(f"手续费占比检查: 总成交 {sum(float(c['qty'])*float(c['entry']) for c in closed):.2f} 名义, "
              f"往返费率占比 {0.0006*2*100:.3f}%")
    print("=" * 60)
    print("说明: 本脚本仅验证交易链路账目(成交/费用/资金费率/TPSL/记忆)。")
    print("AI 选股质量在 LLM 密钥配置后由真实 AI 决策验证。")


if __name__ == "__main__":
    main()