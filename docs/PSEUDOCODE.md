# PSEUDOCODE — bitget-supermarket-trader

主循环(5 分钟一跳, 由 systemd timer 或自身 sleep 驱动):

```
loop:
  run_once()

run_once():
  # ---- 0. 账户与市场状态 ----
  account = bitget.get_account()            # 全仓模式: equity/available
  positions = bitget.get_positions()        # 现存持仓
  reconcile: 交易所侧持仓 vs 本地记忆, 补录被 SL/TP 平掉的仓 (真实PnL)
  apply 熔断状态: 日亏熔断 / 连亏熔断 / 运行时熔断(LLM挂)

  # ---- 1. 管仓 (最高优先级, 每 tick 必跑) ----
  if positions 非空:
    for pos in positions:
      ctx = build_context(pos.SYMBOL, manage=True)   # 现价+K线+指标+费率+账户+教训
      decision = ai.decide_manage(ctx)               # HOLD / ADJUST(sl,tp) / CLOSE / ADD
      if decision == CLOSE:     executor.close(pos, reason=decision.reason)
      elif ADJUST:              executor.update_tpsl(pos, decision)   # 差异>0.2%才重挂
      elif ADD:                 risk.check_add(pos)  → executor.increase(pos)
      # HOLD: 不动

  # ---- 2. 扫描开仓 (满足名义/仓数/熔断约束才扫) ----
  if not paused and equity > 0:
    candidates = risk.pick_pool(contracts, positions)   # 流动性门槛+持仓者优先
    for symbol in rotate(candidates, 每轮≤5):
      ctx = build_context(symbol, manage=False)
      decision = ai.decide_open(ctx)                    # BUY(+sl,tp,notes) / HOLD
      if decision == BUY:
        order = risk.validate_open(decision, symbol, account, positions)
        if order.ok:  executor.open(order)              # 挂TPSL→下单→本地记录
        else:         log_reject(order.reason)

  # ---- 3. 复查与复盘 ----
  if 新平仓数 - review_base >= 5 and 总平仓 >= 3:
    lessons = ai.review(closed_trades, committee_diagnosis)
    memory.save_lessons(lessons)
    review_base = 新平仓数

build_context(symbol, manage):
  klines_5m, klines_1h, klines_4h, klines_1d = bitget.get_klines(symbol)  # 缓存TTL 60s
  quote = bitget.get_quote(symbol); orderbook = bitget.get_orderbook(symbol)
  funding = bitget.get_funding(symbol)
  news = summarize(最近新闻)                       # 事实输入, 非决策规则
  ind = indicators.compute(klines)                 # RSI/MA/ATR/ADX/量比/BB/VWAP/关键位/反转K线/量价
  shape = trend_shape(klines_5m)                   # 近2小时轨迹文本
  history = memory.get_symbol_history(symbol)      # 已平仓结果(防锚定)
  lessons = memory.get_lessons()
  return AIInput(symbol, quote, ind, shape, orderbook, funding, news,
                 account_status, history, lessons)

ai.decide_open(ctx) → {action: BUY|HOLD, entry?: current, stop_loss: $, take_profit: $, reason}
ai.decide_manage(ctx)(持仓) → {action: HOLD|ADJUST|CLOSE|ADD, stop_loss?, take_profit?, reason}

risk.validate_open():
  硬规则(任一不满足→拒绝, 记日志):
  1. equity > 0 且未熔断
  2. 总名义 + 本次名义 ≤ equity × 6
  3. 持仓数 + 1 ≤ max(1, equity // 10)
  4. 该标的已持仓? → 拒绝重复开仓
  5. 本次名义 = min(margin_usd × lev, 可开名义) ; margin_usd ≤ 2, lev ≤ min(20, maxLever)
  6. SL 必填且方向正确; sl_dist ∈ [1%, 12%]; RR = tp_dist/sl_dist ≥ 1.5
  7. 名义 ≥ minTradeUSDT($5) 且为 size 步长的整数倍
执行层下单顺序(全仓):
  a. set_leverage(symbol, lev)                # 幂等
  b. place_tpsl_order(sl, tp) 先挂             # 保护优先
  c. place_order(market/buy, size)            # 成交流水回读核实
  d. memory.record_open(...)

executor.update_tpsl():
  仅当 |新sl−旧sl| / 旧价 > 0.2% 或 |新tp−旧tp| / 旧价 > 0.2% 时重挂(先撤旧再挂新)

executor.close():
  全部平仓: place_order(sell, size, reduceOnly=?)  → 全仓模式用 posSide=long + side=sell
  回读成交; memory.record_close(symbol, price, pnl, reason) → 触发连亏/复盘统计

review.committee_diagnosis(closed):   # 4个判定员(纯计算,无LLM)
  技术(胜率/盈亏比/样本<8警告) 风险(最大连亏/单笔最大亏)
  方向(direction_ok = 最大浮盈≥0.5% → 方向对但止损紧 vs 方向错)
  过拟合(时段胜率<20% 且样本≥3 → 标记)

paper executor:  与 real 同构, 用真实行情模拟成交:
  open: 以当前 ask 成交, 扣 taker 0.06% 费, 记虚拟持仓
  tick: 按 mark 实时计算浮盈亏; funding 结算按真实费率
  close: 以当前 bid 成交, 扣费; 资金费率按 8h 边界同步结算
  TPSL: 模拟触发(价格穿止损价→按 SL 价成交)
```

## 数据流(单向)
```
Bitget API ──► market 聚合 ──► indicators ──► AIInput ──► AI(LLM)
                                                          │(JSON决策)
                                                          ▼
        memory(教训/历史) ◄── review ◄── tracker ◄── executor ◄── risk 校验
                                                          │
                                                    Bitget API(下单)
```