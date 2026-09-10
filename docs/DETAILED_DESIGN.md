# DETAILED_DESIGN — bitget-supermarket-trader

## 1. 模块地图

```
bitget-supermarket-trader/
├── pyproject.toml            # uv 工程
├── config.yaml               # 全部可调参数(默认值=已确认设计)
├── .env                      # 密钥(0600, gitignore)
├── docs/{PLAN,PSEUDOCODE,DETAILED_DESIGN}.md
├── scripts/
│   ├── verify_api.py         # 只读自检: 密钥/账户/合约规格/费率/资金费率
│   └── llm_check.py          # LLM 端点诊断(改自 nautilus-live)
├── src/supermarket/
│   ├── __init__.py
│   ├── config.py             # Config: yaml + env 合并, 强类型
│   ├── bitget_client.py      # BitgetClient: 公开+私有, HMAC 签名(base64)
│   ├── indicators.py         # TechnicalIndicators + trend_shape + reversal_kline
│   ├── market.py             # MarketData: klines/quote/orderbook/funding → AIInput
│   ├── ai.py                 # LLMProvider 接口 + OpenCodeProvider + FallbackHOLD
│   ├── prompts.py            # 超市策略 open/manage/review 三套 prompt
│   ├── risk.py               # RiskEngine: 熔断/名义/仓数/单笔校验
│   ├── execution.py          # PaperExecutor / RealExecutor(共用接口)
│   ├── tracker.py            # 交易记录(JSON, 防崩溃) + PnL 统计
│   ├── memory.py             # AIMemory: decisions/holds/lessons/per-symbol
│   ├── review.py             # 复盘循环 + committee 判定
│   └── engine.py             # SupermarketEngine: run_once 主循环 + CLI
└── tests/
    ├── test_sign.py          # 签名向量
    ├── test_indicators.py    # 指标计算(含已知合成K线黄金值)
    ├── test_risk.py          # 风控拒绝矩阵
    ├── test_ai_parse.py      # JSON 决策解析/容错/截断修复
    └── test_execution.py     # paper 成交/费率/资金费率/TPSL 模拟
```

## 2. config.yaml(默认值)

```yaml
mode: paper                    # paper | live(实盘需显式改+确认)
scan_interval: 1800            # 30 分钟(中线; 周末停机), 每轮15标的
max_symbols_per_round: 15
margin_per_trade_usd: 2.0      # 每仓保证金上限($2)
leverage: 20                   # 目标杠杆; 执行时 min(20, 合约maxLever)
margin_mode: crossed           # 全仓! 用户核心要求
max_notional_mult: 6.0         # 总名义 ≤ 净值 × 6
max_positions_divisor: 10      # 兼容(实际仓位 by risk.py: 净值×6÷每仓名义推导, 硬顶6)
min_turnover_floor: 5000000    # 流动性门槛 24h 成交额 ≥ $5M
sl_min_pct: 2.0  sl_max_pct: 15.0
min_rr: 1.5                    # 止盈/止损 距离比
stop_repost_diff_pct: 0.2      # SL/TP 变更重挂阈值
max_daily_drawdown_pct: 30.0
max_consecutive_losses: 3      # 连亏暂停触发数
pause_after_loss_minutes: 120
llm:
  base_url: ${LLM_BASE_URL}
  api_key:  ${LLM_API_KEY}
  model:    ${LLM_MODEL_FLASH}
  temperature: 0.3
  max_tokens: 1200
  timeout_s: 45
  max_retries: 2
  circuit_failures: 5
  circuit_pause_s: 300
bitget:
  base_url: https://api.bitget.com
  api_key:  ${BITGET_API_KEY}
  secret:   ${BITGET_SECRET_KEY}
  passphrase: ${BITGET_PASSPHRASE}
  timeout_s: 20
state_dir: state               # 决策/持仓/教训 JSON 落盘
paper:
  initial_equity: 50.0         # 虚拟账户 $50 (用户确认; 多头约6仓上限)
  maker_fee: 0.0002
  taker_fee: 0.0006
```

## 3. Bitget v2 接口(已实测验证)

**公开:**
- `GET /api/v2/mix/market/contracts?productType=USDT-FUTURES` → 787 合约,美股品种
  symbolType=perpetual, 字段: minTradeUSDT, maxLever, takerFeeRate, makerFeeRate,
  fundInterval(8h), sizeMultiplier, posLimit, symbolStatus, buyLimitPriceRatio
- `GET /api/v2/mix/market/candles?symbol=NVDAUSDT&productType=USDT-FUTURES&granularity=5m&limit=200`
- `GET /api/v2/mix/market/tickers?productType=USDT-FUTURES`
- `GET /api/v2/mix/market/orderbook?symbol=..&type=step0`
- `GET /api/v2/mix/market/funding-time?symbol=..&startTime&endTime`  → 资金费率历史

**私有(摘要, 签名 = base64(HMAC-SHA256(secret, ts+method+path+body))):**
- `GET /api/v2/mix/account/account?productType=USDT-FUTURES&marginCoin=USDT&symbol=<SYMBOL>`
  → **注意实测: 该端点必须带 symbol**(实测只带 productType 报 400172; 带 symbol 返回
  crossedRiskRate/accountEquity/available/isolatedMaxAvailable 等; 带任意美股 symbol
  返回的是整个 USDT 合约账户权益, 实测 usdtEquity 全账户口径)
- `POST /api/v2/mix/account/set-leverage` {symbol, marginCoin, leverage, holdSide}
- `POST /api/v2/mix/order/place-order`
- `POST /api/v2/mix/order/place-tpsl-order`   # planType: pos_loss/pos_profit
- `POST /api/v2/mix/order/cancel-plan-order`
- `GET /api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT`

**下单(全仓 → 实盘在开启前用只读夹具再验证一遍字段, 旧项目的实时验证在新密钥到位后再做):**
```json
{ "symbol":"NVDAUSDT", "marginCoin":"USDT", "productType":"USDT-FUTURES",
  "marginMode":"crossed", "posSide":"long", "side":"buy",
  "orderType":"market", "size":"0.01", "leverage":"20" }
```

**TPSL(保护优先, 先挂后开):**
```json
{ "symbol":"NVDAUSDT", "productType":"USDT-FUTURES", "marginCoin":"USDT",
  "planType":"pos_loss", "triggerPrice":"...", "executePrice":"...", "holdSide":"long" }
```
重复: 同一个持仓重复挂 TPSL 用 `cancel-plan-order` 撤旧再挂新。

## 4. 指标集(slim, 来自 ai-native-trading skill 的最佳实践)

- **5m**: RSI(14), MA10, MA30, ATR(14), VWAP, 量比, BB 位置
- **1h**: RSI, ADX(14), regime(trend_up/down/flat via ADX)
- **4h 定方向**: RSI, ADX, MA30, MACD(12/26/9)+cross, 量比, BB
- **1d**: 仅 RSI/ADX/MA30(季节背景)
- **trend_shape(5m×24)**: 形态(单边/横盘/反转/加速/衰竭) + 近3根K线 + 波动范围 + 关键高低位
- **反转K线检测(用户核心信号 → 复用旧系统实现)**: 4H 前段涨跌 + 小实体/长影线 +
  量价背离, 并附位置门控(pos_pct>85 顶 / <15 底)
- **量价状态**: 缩量回调=洗盘 / 放量下跌=风险 / 放量上涨=强势 / 缩量上涨=乏力
- **BIAS 乖离**: price/MA 距离(追高判定)

注意 granularity 枚举实测为 `5m`/`1H`/`4H`/`1D` 大小写混合(旧项目验证过), 以
`/api/v2/mix/market/candles` 实测返回为准, 测试里固化。

## 5. 风控引擎(代码硬约束 = 法律; prompt = 建议)

| 校验 | 规则 |
|---|---|
| `check_open(signal, account, positions, contract)` | 见下 |
| 账户 | equity > 0; 未处熔断(日亏/连亏/LLM 熔断) |
| 名义 | `(总名义 + Δ) ≤ equity × max_notional_mult` |
| 仓数 | `len(positions)+1 ≤ max(1, floor(equity/10))` |
| 重复 | symbol 未持仓 |
| 单笔 | margin = min($2, equity×0.6), 名义=margin×min(20,maxLever); 名义≥minTradeUSDT |
| SL | 必填; buy 仓 sl<entry; sl_dist∈[1%,12%]; RR≥1.5(不足→升级提示, 不足1→拒) |
| 熔断 | 日累计亏损≥30% 净值 → 当日禁开新仓; 连亏3 → pause 2h |

熔断状态持久化到 state/(json), 重启不丢。停机时段(周六日/休市)仍可跑:
K线照常, 仓位管仓仅评估不调 TPSL(旧系统教训: 周末调 TPSL 无意义)。

## 6. AI 决策层

- `LLMProvider` 抽象: `decide_open(AIInput) -> OpenDecision` / `decide_manage(...)` /
  `review(closed, committee) -> [lessons]`
- `OpenCodeProvider`: OpenAI 兼容 chat/completions, `temperature=0.3`, `max_tokens=1200`,
  `json_mode=False`(旧系统验证: flash 用 json_mode 会截断), prompt 末尾
  "输出JSON, 不要思考, 直接给结果", 容错解析(截断 JSON 补括号/提取首个 {..}),
  hit 超时重试(5xx/网络重试, 4xx 不重试), class-level 熔断(连续5失败→停300s)
- 任何失败 → 该标的本次决策 = HOLD(不开仓 = 最安全), 记日志
- `FallbackHOLDProvider`: LLM 不可配/密钥缺失时兜底, 恒 HOLD(开发期/只读验证可用)

**AIInput(事实输入, 全部为数据, 无规则):**
```
{symbol, quote{last,bid,ask,chg_24h}, session, klines5m简述+ind5m,
 ind1h, ind4h(定方向), ind1d, trend_shape, orderbook压力, funding(费率+结算时间),
 news摘要, account{净值,可用,已用保证金,名义敞口,仓数,今日盈亏}, history, lessons}
```

**Open prompt 结构(超市策略, 中文):**
```
角色: 美股超市买手。目标: 在上涨周期/可能上涨的股票上小额建仓($40名义/仓),
      赚1-3%兑现, 亏损单视趋势决定拿住或认错, 整体净值增长。
一、方向(4H定方向, 1H辅助): 趋势向上才考虑买; 横盘/下降不买
二、买点: 回踩企稳 / 趋势中继突破 / 强股回调到位(VWAP/MA10支撑)
三、超市纪律: 1) 涨1-3%可卖(快进快出) 2) 亏损<2% 且趋势未坏 → 拿住等回涨
      3) 止损条件(任一): 跌破关键位+放量 / 4H趋势转坏 / 突发利空 / 亏损>4% → 认错
      4) 不在高位追(远离VWAP>1.5ATR 或 当日已涨>8% 等回踩) 5) 资金费率考虑
四、输出 JSON: {"action":"BUY|HOLD","stop_loss":价格,"take_profit":价格,"reason":"..."}
```

**Manage prompt(持仓管仓):** 每个持仓独立评估 → HOLD(可含新SL/TP)/ CLOSE / ADD(不加仓, 本轮禁ADD,
简化) ; 引用趋势形态+4H方向+量价, 分级: 浮盈≥0.5%→保本, ≥1.5%→可兑现; 亏损认错标准同上。
周末/休市: 仅评估不更TPSL。

**Review prompt:** 只输出 lessons(参考性经验, 禁止"禁止类"规则), 必须与 committee 诊断吻合,
条件化描述("什么情况下容易发生什么")。教训注入两个决策 prompt。

**防锚定:** history 只注入已平仓结果, 并附"行情会反转, 以当前数据为准"。

## 7. 执行层

- `Executor` 接口: `open(signal)`, `manage_tpsl(pos, sl, tp)`, `close(pos, reason)`,
  `positions()`, `account()` — paper/real 同构
- **RealExecutor 下单顺序**: set_leverage → place-tpsl(先保护) → place-order → 回读持仓核实
  → tracker+memory 落盘。任何一步失败 → 撤单回滚(撤 TPSL) + REJECT 日志
- **交易所触发的 SL/TP 平仓**: get_positions 差异检测 + 启动对账补录(旧系统痛点, 直接吸收)
- **PaperExecutor**: 虚拟 equity(默认 $30), 真实 ask/bid 成交 + 真实费率; 资金费率
  按真实 funding rate 在 8h 边界结算; TPSL 用 tick 价格穿越判定(保守: 止损用穿价成交价,
  止盈用目标价); 状态全落盘可复盘

## 8. 记忆/复盘

- `tracker.json`: 所有订单/成交/平仓(含 reason), 崩溃安全(每笔原子写临时文件+rename)
- `ai_memory.json`: decisions[](开仓动作, ≤500), holds[](仅审计, ≤200), lessons[],
  per-symbol 历史由 decisions 派生
- 复盘触发器: 平仓数 − review_base ≥ 5 且 总平仓 ≥ 3(Persisted base, 重启不丢)
- direction_ok 判定(吸收 T+N 评估): max 浮盈 ≥0.5% → 方向对
- 委员会诊断(纯计算)与 LLM lessons 双轨, 诊断作为事实上下文

## 9. 运行方式

```bash
uv sync
cp .env.example .env; chmod 600 .env   # 填入密钥(新 Bitget 密钥/LLM 密钥)
scripts/verify_api.py                  # 只读自检
uv run python -m supermarket.engine --mode paper --once   # 单次跑通
uv run python -m supermarket.engine --mode paper          # 持续运行(前台)
# systemd: supermarket-paper.service / supermarket-live.service(显式确认后启用)
```

实盘解锁条件(代码强制): (a) mode=live (b) 环境变量 LIVE_CONFIRM=yes 由用户在开启时刻设置
(c) verify_api 通过 (d) 账户 equity > 0 且 ≥ 5 美元。三者缺一 → 启动即失败并说明。