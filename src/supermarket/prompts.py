"""超市策略 Prompt 构建(开仓/管仓/复盘三套)。

设计要点(来自 ai-native-trading skill 的实战教训):
- 数据即事实: 渲染全部行情数据, AI 是唯一决策者, 无规则预筛
- 全仓模式数学: 名义敞口/爆仓线/手续费/资金费率是 AI 决策必须见到的成本
- 复盘只出 lessons(参考性经验), 绝不生成"禁止类"规则
- 防锚定: 历史只显示已平仓结果 + 显式"以当前数据为准"
"""

from __future__ import annotations

from typing import Any

SYSTEM_OPEN = """你是美国股票永续合约的"超市买手"交易员。操作原则与超市进货一致:
买入价格合理、处于上涨周期或大概率上涨的股票, 盈利时快速兑现, 少量亏损单在趋势未坏时
可以持有等待回涨, 趋势明显变坏时果断认错止损。同时允许少量"高位做空": 股票上涨受挫
开始下跌时顺势做空, 既赚下跌的钱, 也为多单组合提供对冲, 更不容易爆仓——但空单最多给
1~2 个, 是辅助不是主力。

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
**止损(超市呼吸空间)**: 止损放关键支撑位(结构低点/MA30)外侧 + ATR 缓冲, 正常给 2%~10%
空间, 少数波动大的强势股可到 15%——不要用窄止损, 正常波动不该扫掉超市的囤货;
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
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
    lines.append(inp.ind_1d_line)   # 日线定方向, 放最前
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
    if inp.lessons:
        lines.append("历史经验(参考): " + "；".join(inp.lessons))

    lines.append("")
    lines.append("决策步骤(内心完成, 不输出): "
                 "① 日线方向(定方向!): 趋势向上→只考虑BUY(低吸); 趋势向下→考虑SELL(反抽/破位); "
                 "日线趋势衰竭(ADX<20)→看反转K线信号. "
                 "② 现在的位置是入场点吗(回踩企稳买/反弹失败空, 而非追高追跌)? "
                 "③ 止损放哪(2%~10%, 结构位外侧给呼吸空间), 止盈对应盈亏比是否≥1.5(扣手续费后仍赚)? "
                 "④ 若做空: 确认这是'上涨受挫开始下跌'的顺势空, 且整体空仓数≤2个. "
                 "⑤ 该股符合'超市进货'标准吗——价格合理、有动能、题材/新闻支持?")
    lines.append("判断完毕直接输出JSON。")
    return "\n".join(lines)


SYSTEM_MANAGE = """你是持仓管理AI(超市店长)。为每个持仓独立决策, 分多头/空头。

多头原则:
1. 日线方向对照: 日线趋势转为明确向下(ADX≥25)→ 认错离场; 日线仍向上 → 正常的日内回调
   可以拿住等回涨(超市理论: 没买在高位的正常股票一段时间会涨回来)。
2. 中线超市不急于锁利: 浮盈1-3%属正常波动, 继续持有, **不要因小浮盈上移止损**
   (那会让正常回调扫掉你的仓位); 止损始终按结构位(关键支撑/MA下方)设置并给呼吸空间;
   浮盈≥5% 或接近止盈目标 或 日线动能明显减弱 时, 才考虑兑现(CLOSE)或上移止损保护利润。
3. 亏损<8%且日线未坏、没买在高位 → 拿住等回涨(超市核心: 给足呼吸空间, 用户容忍度
   10-15%深回调可拿); 亏损<8%但明显买错(追高被套/日线转坏苗头) → 认错(CLOSE);
   亏损≥10% → 必须认真考虑止损(CLOSE), 亏损≥15%仍不止损=失控立即离场。

空头原则(镜像):
1. 日线方向对照: 持仓的日线趋势转为明确向上(ADX≥25)→ 认错离场(空单别扛反弹);
   日线仍向下 → 正常的反弹可以拿住等回跌。
2. 中线不急于锁利: 浮盈1-3%属正常波动, 继续持有, **不要因小浮盈下调止盈/移动止盈**
   (那会让正常反弹扫掉你的仓位); 止损始终按结构位(关键阻力/MA上方)设置并给呼吸空间;
   浮盈≥5% 或接近止盈目标 或 下跌动能明显减弱(放量长下影/RSI超卖) 时, 才考虑兑现或移动止盈。
3. 亏损(价格上涨)<8%且日线未坏 → 可拿住; 亏损<8%但明显空错(抄顶被套) → 认错(CLOSE);
   亏损≥10% → 必须认真考虑止损(CLOSE), 亏损≥15%仍不止损=失控立即离场。

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
(注: 已深跌40%+的票继续大幅下探概率反而低(83%不再亏超15%), 但那是对风险的描述, 不是买入理由。)

输出格式(严格JSON, 直接给结果):
{"action":"HOLD"或"ADJUST"或"CLOSE","stop_loss":止损价或null,"take_profit":止盈价或null,"reason":"50字内中文理由"}
HOLD=保持现状或给出期望的新价位(会在差异超过0.2%时执行), CLOSE=立即平仓。"""


def build_manage_prompt(inp: "AIInput", pos: dict[str, Any]) -> str:
    pnl_pct = float(pos.get("unrealized_pnl_pct", 0))
    entry = float(pos.get("avg_entry", 0))
    last = float(inp.quote.get("lastPr", 0))
    direction = str(pos.get("direction", "long"))
    dir_label = "多头" if direction == "long" else "空头"
    lines = [
        f"持仓: {inp.symbol} [{dir_label}] 开仓价${entry:.2f}  现价${last:.2f}  "
        f"浮盈亏{pnl_pct:+.2f}%  名义${float(pos.get('notional', 0)):.1f}",
        f"账户: {_render_account(inp.account)}",
    ]
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
    lines.append(inp.ind_1d_line)
    lines.append(inp.ind_4h_line)
    lines.append(inp.ind_1h_line)
    lines.append(inp.ind_5m_line)
    lines.append(f"走势形态: {inp.trend}")
    if inp.orderbook:
        lines.append(f"盘口: {inp.orderbook}")
    if inp.news:
        lines.append(f"新闻(事实参考): {inp.news}")
    if inp.lessons:
        lines.append("历史经验(参考): " + "；".join(inp.lessons))
    lines.append("")
    lines.append("决策步骤(内心完成): ① 日线方向还对吗(持仓方向对照) ② 位置止损清晰吗 "
                 "③ 盈利兑现还是拿住 ④ 亏损是'正常回调可等回涨'还是'买错要认错' ⑤ 资金费率/时间成本")
    lines.append("判断完毕直接输出JSON。")
    return "\n".join(lines)


SYSTEM_REVIEW = """你是交易复盘教练。根据最近的真实交易结果(含盈亏数据), 输出可复用的经验教训。

要求:
- 只输出"经验教训"(描述性, 参考性质), 不要输出任何"禁止/必须"类的规则或命令。
- 经验要具体: "什么情况下容易发生什么", 例如"追高成交量大涨股容易被反转扫损"。
- 教训必须与事实诊断吻合(诊断给出的胜率/盈亏比/方向正确率等)。
- 最多6条, 每条60字以内, 中文。
- 如果样本太少(<5笔)或者没有亏损样本, 输出空列表。

输出格式(严格JSON): {"lessons":["...", "..."]}"""


def build_review_prompt(closed: list[dict[str, Any]], diagnosis: str) -> str:
    rows = []
    for c in closed[-25:]:
        rows.append(
            f"{c.get('symbol')} {c.get('action')} 入${float(c.get('entry',0)):.2f} "
            f"出${float(c.get('exit',0)):.2f} pnl${float(c.get('pnl',0)):+.3f} "
            f"({c.get('close_reason','')}) 最大浮盈{float(c.get('max_pnl_pct',0)):+.1f}%"
        )
    return (
        "最近真实交易记录:\n" + "\n".join(rows) +
        "\n\n事实诊断(委员会):\n" + diagnosis +
        "\n\n请输出JSON {\"lessons\":[...]}。"
    )


# 委员会诊断(纯计算, 无LLM) — 与 review 配合
def committee_diagnosis(closed: list[dict[str, Any]]) -> str:
    if not closed:
        return "无已平仓样本"
    total = len(closed)
    wins = [c for c in closed if float(c.get("pnl", 0)) > 0]
    losses = [c for c in closed if float(c.get("pnl", 0)) <= 0]
    wr = len(wins) / total * 100
    gross_w = sum(float(c["pnl"]) for c in wins)
    gross_l = sum(-float(c["pnl"]) for c in losses)
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    max_loss = min((float(c["pnl"]) for c in closed), default=0)
    # 最大连亏
    streak = 0
    max_streak = 0
    for c in sorted(closed, key=lambda x: x.get("close_ts", 0)):
        if float(c.get("pnl", 0)) <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    dir_ok = sum(1 for c in closed if float(c.get("max_pnl_pct", 0)) >= 0.5)
    dir_pct = dir_ok / total * 100 if total else 0
    parts = [
        f"样本{total}笔, 胜率{wr:.0f}% ({len(wins)}胜/{len(losses)}负)",
        f"盈亏比{pf:.2f} (毛盈${gross_w:.2f}/毛亏${gross_l:.2f})",
        f"最大连亏{max_streak}, 单笔最大亏损${max_loss:.2f}",
        f"方向正确率{dir_pct:.0f}% (最大浮盈≥0.5%视为方向对)",
    ]
    if total < 8:
        parts.append("样本<8, 结论置信度低")
    if dir_pct < 40 and total >= 3:
        parts.append("方向正确率过低→入场问题(选股/买点)")
    elif dir_pct >= 60 and losses:
        parts.append("方向对但亏损→止损/持有问题(止损过紧或离场过早)")
    if max_streak >= 4:
        parts.append("连亏≥4→存在系统性风险, 需检查是否追高/逆势")
    return ", ".join(parts)