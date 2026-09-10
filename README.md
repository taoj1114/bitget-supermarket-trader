# bitget-supermarket-trader

Bitget 美股永续合约"超市策略"AI 交易系统。

- **全仓(crossed)保证金**,每仓 $2 保证金 × 20倍 = $40 名义
- **只做多**,选取上涨周期/可能上涨的美股,赚 1~3% 快进快出
- **亏损单**:趋势未坏→拿住等回涨(全仓扛得住);趋势变坏→AI 认错止损
- **AI 是唯一决策者**:指标/K线/盘口/资金费率只是事实输入,程序只做风控和执行约束
- **风控硬约束**:总名义 ≤ 净值×6(爆仓线≈-16%)、仓数 ≤ 净值/10、每仓必挂交易所 TPSL、
  日亏 30% 熔断、连亏 3 次暂停 2 小时

## 快速开始

```bash
cd /root/workspace/bitget-supermarket-trader
cp .env.example .env && chmod 600 .env
# 编辑 .env: BITGET_* + LLM_* 三个密钥
.venv/bin/python scripts/verify_api.py     # 只读自检(账户/池/费率/LLM)
.venv/bin/python scripts/llm_check.py      # LLM 连通检查
.venv/bin/python -m supermarket.engine --mode paper --once   # 单轮跑通
.venv/bin/python -m supermarket.engine --mode paper          # 持续运行(纸面)
```

## 实盘解锁(缺一不可, 全部由代码强制)

1. `.env` 中 `LIVE_CONFIRM=yes`(显式)
2. 账户已充值(余额 > 0,建议 $20-50 起)
3. `scripts/verify_api.py` 全部 OK
4. 已配置有效 LLM 密钥(未配置→自动 HOLD 不开仓)

满足后: `systemctl link deploy/supermarket-live.service && systemctl enable --now supermarket-live`

## 策略文档

- docs/PLAN.md — 目标与风险数学模型
- docs/PSEUDOCODE.md — 主循环伪代码
- docs/DETAILED_DESIGN.md — 模块设计/接口/风控矩阵

## 状态与数据(都在 state/, gitignore)

- `state/paper.json` / `real.json` — 持仓与成交
- `state/ai_memory.json` — AI 决策/教训/复盘基线(重启不丢)
- `state/breakers.json` — 熔断状态(日基线/连亏)
- `state/trader_state.json` — 每轮引擎状态快照

## 测试与链路验证

```bash
.venv/bin/python -m pytest tests/ -q          # 40 个单元测试
.venv/bin/python scripts/simulate_paper.py    # 真实行情链路验证(测试决策器, 非策略)
```

## 安全边界

- 密钥只存在于 .env(0600), 聊天/日志永不明文; 暴露即作废
- 系统默认 paper;live 需要三层显式确认
- 入池美股 212 个:isRwa=YES - ETF黑名单(50) - 黄金/商品 - 非美股(港股日股/指数/外汇)