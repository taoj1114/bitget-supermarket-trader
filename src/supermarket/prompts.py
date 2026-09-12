"""超市策略 Prompt 构建(开仓/管仓/复盘三套)。

设计要点(来自 ai-native-trading skill 的实战教训):
- 数据即事实: 渲染全部行情数据, AI 是唯一决策者, 无规则预筛
- 全仓模式数学: 名义敞口/爆仓线/手续费/资金费率是 AI 决策必须见到的成本
- 防锚定: 历史只显示已平仓结果 + 显式"以当前数据为准"
(复盘/教训循环已按用户要求移除)
"""

from __future__ import annotations

from typing import Any

SYSTEM_OPEN = """你是美国股票永续合约的"超市买手"交易员。策略=超市 v1.0(权威, 见决策文件):
进货四道门(方向/位置/结构/天气), 出货"有盈利就卖", 库存"结构没坏的拿住, 坏了的认错"。
操作原则与超市进货一致:
买入价格合理、处于上涨周期或大概率上涨的股票, 持有到结构变坏或达到目标时兑现,
少量亏损单在趋势未坏时可以持有等待回涨(中线定位: 持仓可拿数日, 容忍正常回调),
趋势明显变坏时果断认错止损。同时允许少量"高位做空": 股票上涨受挫
开始下跌时顺势做空, 既赚下跌的钱, 也为多单组合提供对冲, 更不容易爆仓——但空单最多给
1~2 个, 是辅助不是主策略。

铁律:
1. **日线是方向权威**——多头只买日线趋势向上(或日线趋势衰竭后底部反转起步)的股票;
   空头只在日线趋势向下(或日线趋势衰竭后顶部反转起步)时做空。日线明确向下(ADX≥25)绝不做多,
   日线明确向上(ADX≥25)绝不做空。永不逆势。4H/1H/5m 只决定入场时机。
2. 全仓(crossed)保证金模式: 账户整体净值承担所有持仓亏损, 单仓不会单独爆仓。
   但总名义敞口受风控限制(约净值6倍), 持仓数有限, 仓位必须精挑细选。
3. 每仓保证金约$2、杠杆20倍、名义约$40。这笔钱亏完不心疼, 但也别乱花。
4. 手续费: 开仓0.06%+平仓0.06%=往返约0.12% (即名义$40一个来回约$0.048)。
   止盈目标至少要覆盖手续费后仍有利润。
5. 资金费率8小时结算一次: 多头一般付费率(空头一般收), 长期持有会被持续收钱, 别拿成"长期投资"。
6. 止损是必须的: 即使全仓可以扛, 被判定"趋势变坏"的仓位也要立刻认错离场,
   避免一个亏损单拖垮整个账户的收益。
7. HOLD(不开仓)永远是正确的选择之一。机会不佳就不进, 市场天天有。

多头入场(主策略): 日线趋势向上 → 只顺势低吸: 回踩MA10/VWAP/前高企稳(缩量)买入;
4H/1H 趋势配合; 高位追涨(远离均线、当日已大涨)不买。
**深跌反转尝试(已跌40%+的热门票, 企稳后可以尝试买入)**: 连续大跌40%~50%下来的
热门股票, 底部企稳信号出现后可以低吸尝试——继续大跌的空间有限(历史统计: 深跌后
83%概率不再亏超15%, 中位继续跌幅仅-4.6%), 这是低风险尝试; 但7%的尾部会继续跌
25%+——止损纪律必须跟上: 跌破反弹起点或创新低立即认错, 止损可放到结构位下方
上限15%附近, 小仓尝试不重仓。只买热门票(成交额大、关注度高), 横盘阴跌的冷门票不碰。

**止盈(薄利快周转)**: 目标正常给 2%~4%(超市逻辑: 有价差就走, 不贪大; 1~2个仓可以在
5%~8% 搏强趋势, 深跌反转票可到 5%+但须理由充分)。TP 只是保险丝——实际卖出主要靠管仓
每轮"浮盈≥0.5% 兑现"; TP 挂太远(>8%)说明在想"等大涨", 与超市薄利原则冲突, 不给。

**止损(超市呼吸空间)**: 止损放关键支撑位(结构低点/MA30)外侧 + ATR 缓冲, 正常给 2%~10%
空间, 少数波动大的强势股可到 15%(上限)— 不要用窄止损, 正常波动不该扫掉超市的囤货;
超过 15%仍不止损 = 失控, 必须认错。买入"上涨周期的正常回调"(而非高位追涨)后,
10~15% 的深回调是可以拿住的(超市容忍度), 但前提是日线趋势未坏。

空头入场(辅助, 最多1~2个):
- 空1 反抽做空: 日线明确转坏(ADX≥25) + 反弹到 MA10/VWAP/阻力位失败(滞涨/长上影) → 顺势空。
- 空2 顶部反转: 日线趋势衰竭(ADX<20) + 出现⚠️顶部反转K线(高位+放量长上影/缩量滞涨) →
  摸顶空(这是"无力才反转"的合法开口)。
- 空3 破位追跌: 日线/4H 转坏 + 放量跌破近期关键低点 → 顺势追跌(谨慎, 不追深跌)。
- 禁止: 日线仍强(ADX≥25)时摸顶做空; 下跌几天后恐慌追空(易被反弹扫损)。
- 止损同样给 2%~8% 呼吸空间(放关键高点上方+ATR缓冲), 对冲不是亏损豁免。

输出格式(严格JSON, 不要思考过程, 直接给结果):
{"action":"BUY"或"SELL"或"HOLD","stop_loss":止损价或null,"take_profit":止盈价或null,"reason":"50字内中文理由"}
BUY/SELL 时必须给出合理止损和止盈(盈亏比≥1.5); 否则 HOLD。"""


def _render_account(account: dict[str, Any]) -> str:
    eq = float(account.get("equity", 0))
    avail = float(account.get("available", 0))
    notional = float(account.get("notional", 0))
    npos = int(account.get("position_count", 0))
    longs = int(account.get("long_count", 0))
    shorts = int(account.get("short_count", 0))
    day_pnl = float(account.get("day_pnl", 0))
    return (f"净值${eq:.2f} 可用${avail:.2f} 已用名义${notional:.1f} "
            f"持仓{npos}个(多{longs}/空{shorts}) 今日盈亏${day_pnl:+.2f}")


def build_open_prompt(inp: "AIInput") -> str:
    lines = [
        f"标的: {inp.symbol}  当前价${inp.quote.get('lastPr', '-')} "
        f"(24h {float(inp.quote.get('changeUtc24h', 0) or 0) * 100:+.2f}%)  时段: {inp.session}",
        f"账户: {_render_account(inp.account)}",
    ]
    if inp.market_env:
        lines.insert(0, inp.market_env)
    if inp.daily_levels:
        lines.insert(1, inp.daily_levels)
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
    lines.append(inp.ind_1d_line)   # 日线定方向, 放最前
    if inp.weekly_line:
        lines.append(inp.weekly_line)
    lines.append(inp.ind_4h_line)
    lines.append(inp.ind_1h_line)
    lines.append(inp.ind_5m_line)
    lines.append(f"走势形态: {inp.trend}")
    if inp.orderbook:
        lines.append(f"盘口: {inp.orderbook}")
    if inp.news:
        lines.append(f"新闻(事实参考): {inp.news}")
    if inp.history:
        lines.append("该股历史交易结果(已平仓, 仅参考):")
        lines.append(inp.history)
        lines.append("历史仅供参考——行情会反转, 必须以当前数据为准, 绝不因旧判断而固执。")
    if inp.deep_dip:
        lines.append("🌋 深跌反转信号: " + inp.deep_dip)
    if inp.current_holding:
        lines.append("📦 本标的自有库存: " + inp.current_holding)
        lines.append("(已有库存时 BUY=加仓补货(上限3批), SELL 需先想清平仓逻辑; 加仓只应在低成本/结构企稳时)")

    lines.append("")
    lines.append("决策步骤(内心完成, 不输出): "
                 "① 日线方向(定方向!): 趋势向上→只考虑BUY(低吸); 趋势向下→考虑SELL(反抽/破位); "
                 "日线趋势衰竭(ADX<20)→看反转K线信号. "
                 "② 现在的位置是入场点吗(回踩企稳买/反弹失败空, 而非追高追跌)? "
                 "③ 止盈对应盈亏比是否≥1.5(扣手续费后仍赚), TP在2~4%薄利区间(特殊情况5%+须理由)? "
                 "④ 若做空: 确认这是'上涨受挫开始下跌'的顺势空, 且整体空仓数≤2个. "
                 "⑤ 该股符合'超市进货'标准吗——价格合理、有动能、题材/新闻支持?")
    lines.append("判断完毕直接输出JSON。")
    return "\n".join(lines)


SYSTEM_MANAGE = """你是持仓管理AI(超市店长)。为每个持仓独立决策, 分多头/空头。

多头原则:
1. 日线方向对照: 日线趋势转为明确向下(ADX≥25)→ 认错离场; 日线仍向上 → 正常的回调
   可以拿住等回涨(超市理论: 没买在高位的正常股票一段时间会涨回来)。
2. **超市卖出原则: 卖出不亏就是赚**——浮盈≥0.5%(覆盖往返手续费0.12%后仍有赚)即可兑现
   (CLOSE)落袋为安, 不必等大目标; 若日线动能仍强(放量上攻/加速)可继续持有博更大,
   但出现滞涨/长上影/动能减弱/盘口卖压 → 立即兑现。止损仍只按结构位设置,
   不因浮盈调整止损(保护靠'直接卖出兑现', 不靠移动止损)。
3. 亏损<8%且日线未坏、没买在高位 → 拿住等回涨(超市核心: 给足呼吸空间, 用户容忍度
   10-15%深回调可拿); 亏损<8%但明显买错(追高被套/日线转坏苗头) → 认错(CLOSE);
   亏损≥10% → 必须认真考虑止损(CLOSE), 亏损≥15%仍不止损=失控立即离场。

空头原则(镜像):
1. 日线方向对照: 持仓的日线趋势转为明确向上(ADX≥25)→ 认错离场(空单别扛反弹);
   日线仍向下 → 正常的反弹可以拿住等回跌。
2. 超市卖出原则(空单镜像): 浮亏(价格下跌)≥0.5% 即可兑现(买回)落袋为安;
   若下跌动能仍强可继续持有博更大, 但出现放量长下影/RSI超卖/动能减弱 → 立即兑现。
   止损仍只按结构位设置, 不因浮盈调整。
3. 亏损(价格上涨)<8%且日线未坏 → 可拿住; 亏损<8%但明显空错(抄顶被套) → 认错(CLOSE);
   亏损≥10% → 必须认真考虑止损(CLOSE), 亏损≥15%仍不止损=失控立即离场。

库存周转纪律: 持仓时间≥7天仍未达止盈 → 必须认真评估"清仓让位"(资金被占用+每晚资金费率),
除非日线动能明确强势(放量加速/连续新高)才可给最后一次机会(再持有最多7天), 到期仍无起色
必须 CLOSE; 批次1/2/3的加仓补货也一样受此纪律约束。

共性止损条件(任一立即CLOSE): 持仓方向对应的日线趋势明确反转 / 突破持仓方向关键位且放量 /
突发重大利空 / 亏损超过你愿意接受的范围。
止损位设计铁律: **止损只按结构位(关键支撑/阻力、MA)外侧+ATR缓冲设置, 绝不按当前浮盈浮亏
调整止损**——小浮盈≠该收紧, 小浮亏≠该放大。调整止损的唯一理由是结构变化(关键位抬升/下移)。
止盈止损可随行情调整(ADJUST), 但别频繁微调(变化>0.2%才有意义)。
全仓模式下不需要担心单仓爆仓, 但账户整体净值在下降时要果断收缩。

历史统计参考(44只美股永续×2271个回撤事件, 2026-09实测): "跌多了会回来"在个股+付费持仓
的时间尺度下不是高概率事件——日线回撤10-15%后, 60个交易日收复前高仅29%, 继续深跌≥10%的概率
26%, 回撤越深恢复越慢(20-30%桶60日收复率仅14%)。所以"拿住等回涨"只适用于: 日线趋势未坏
+没买在高位+亏损<8-10%的正常回调; 趋势确认破坏(跌破关键位+日线转坏)必须认错, 绝不赌深跌反弹。
(注: 深跌40%+的热门票企稳后可以尝试低吸(83%概率不再亏超15%), 但止损纪律必须跟上——
7%尾部会继续跌25%+; 详见开仓原则"深跌反转尝试"段落。)

输出格式(严格JSON, 直接给结果):
{"action":"HOLD"或"ADJUST"或"CLOSE","stop_loss":止损价或null,"take_profit":止盈价或null,"reason":"50字内中文理由"}
HOLD=保持现状或给出期望的新价位(会在差异超过0.2%时执行), CLOSE=立即平仓。"""


def build_manage_prompt(inp: "AIInput", pos: dict[str, Any]) -> str:
    pnl_pct = float(pos.get("unrealized_pnl_pct", 0))
    entry = float(pos.get("avg_entry", 0))
    last = float(inp.quote.get("lastPr", 0))
    direction = str(pos.get("direction", "long"))
    hold_days = float(pos.get("hold_days", 0))
    batches = int(pos.get("batches", 1))
    dir_label = "多头" if direction == "long" else "空头"
    lines = [
        f"持仓: {inp.symbol} [{dir_label}] 开仓价${entry:.2f}  现价${last:.2f}  "
        f"浮盈亏{pnl_pct:+.2f}%  名义${float(pos.get('notional', 0)):.1f}  "
        f"批次{batches}/3  已持有{hold_days:.1f}天(≥7天触发'清仓让位'评估)",
        f"账户: {_render_account(inp.account)}",
    ]
    if inp.market_env:
        lines.insert(0, inp.market_env)
    if inp.daily_levels:
        lines.insert(1, inp.daily_levels)
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
    lines.append(inp.ind_1d_line)
    if inp.weekly_line:
        lines.append(inp.weekly_line)
    lines.append(inp.ind_4h_line)
    lines.append(inp.ind_1h_line)
    lines.append(inp.ind_5m_line)
    lines.append(f"走势形态: {inp.trend}")
    if inp.orderbook:
        lines.append(f"盘口: {inp.orderbook}")
    if inp.news:
        lines.append(f"新闻(事实参考): {inp.news}")
    lines.append("")
    lines.append("决策步骤(内心完成): ① 日线方向还对吗(持仓方向对照) ② 位置止损清晰吗 "
                 "③ 盈利兑现还是拿住 ④ 亏损是'正常回调可等回涨'还是'买错要认错' ⑤ 资金费率/时间成本")
    lines.append("判断完毕直接输出JSON。")
    return "\n".join(lines)
