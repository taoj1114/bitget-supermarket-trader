# bitget-supermarket-trader

Bitget 美股永续合约"超市策略"AI 交易系统。

- **全仓(crossed)保证金**,每批 $2 保证金 × 20倍 ≈ $40 名义;同一标的最多 **3 批**(补货)
- **只做多为主**,选取上涨周期/可能上涨的美股;空单 ≤2 个作对冲
- **出货**:有盈利就卖(浮盈 ≥0.5% 动能减弱即兑现,"卖出不亏就是赚");TP 2~4% 只是保险丝
- **补货**(分批建仓):金字塔式,浮盈 ≥0.5% 才补(浮亏禁止摊平);止损只收紧不放宽
- **亏损单**:趋势未坏→拿住等回涨(全仓扛得住,容忍 10-15% 回调);日线转坏→AI 认错离场
- **库存周转**:持仓 ≥7 天未达止盈 → 强制评估"清仓让位"
- **天气门**:SPY 24h 跌 >3% 当日禁开新仓(系统性雨天不开门)
- **AI 是唯一决策者**:指标/K线/盘口/资金费率/大盘只作事实输入,程序只做风控与执行约束
- **风控硬约束**:总名义 ≤ 净值×6(爆仓线≈-16%)、多头 ≤6 标的、空头 ≤2、SL 2%~15%、
  RR≥1.5、每仓必挂交易所 TPSL、日亏 30% 熔断、连亏 3 次暂停 2 小时

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

- **docs/STRATEGY.md — 策略 v1.0 权威(进货四道门/出货/库存/风控, 一切以此为准)**
- docs/PLAN.md — 目标与风险数学模型
- docs/PSEUDOCODE.md — 主循环伪代码
- docs/DETAILED_DESIGN.md — 模块设计/接口/风控矩阵

## 数据真实性与指标研究(可复跑)

```bash
.venv/bin/python scripts/verify_data_truth.py     # 数据真实性: OHLC一致性/时间/量/指标黄金值
.venv/bin/python research/indicators_eval.py      # 指标有效性: 120标的 feature×fwd胜率桶 → 报告
```

## 状态与数据(都在 state/, gitignore)

- `state/paper.json` / `real.json` — 持仓与成交(账目闭环: equity=初始+Σ净pnl-持仓未结费用)
- `state/ai_memory.json` — AI 决策/品种历史记录(重启不丢)
- `state/breakers.json` — 熔断状态(日基线/连亏)
- `state/trader_state.json` — 每轮引擎状态快照

## 测试与链路验证

```bash
.venv/bin/python -m pytest tests/ -q          # 64 个单元测试
.venv/bin/python scripts/simulate_paper.py    # 真实行情链路验证(测试决策器, 非策略)
```

## 安全边界

- 密钥只存在于 .env(0600), 聊天/日志永不明文; 暴露即作废
- 系统默认 paper;live 需要三层显式确认
- 入池美股 212 个:isRwa=YES - ETF黑名单(50) - 黄金/商品 - 非美股(港股日股/指数/外汇)