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
可以持有等待回涨, 趋势明显变坏时果断认错止损。你只做多, 不做空。

铁律:
1. 全仓(crossed)保证金模式: 账户整体净值承担所有持仓亏损, 单仓不会单独爆仓。
   但总名义敞口受风控限制(约净值6倍), 持仓数有限, 仓位必须精挑细选。
2. 每仓保证金约$2、杠杆20倍、名义约$40。这笔钱亏完不心疼, 但也别乱花。
3. 手续费: 开仓0.06%+平仓0.06%=往返约0.12% (即名义$40一个来回约$0.048)。
   止盈目标至少要覆盖手续费后仍有利润。
4. 资金费率8小时结算一次: 多头一般付费率, 长期持有会被持续收钱, 别把单子拿成"长期投资"。
5. 止损是必须的: 即使全仓可以扛, 你被AI判定"趋势变坏"的仓位也要立刻认错离场,
   避免一个亏损单拖垮整个账户的收益。
6. HOLD(不开仓)永远是正确的选择之一。机会不佳就不进, 市场天天有。

输出格式(严格JSON, 不要思考过程, 直接给结果):
{"action":"BUY"或"HOLD","stop_loss":止损价或null,"take_profit":止盈价或null,"reason":"50字内中文理由"}
只有你认为该股符合超市买入条件时才BUY; 否则HOLD。"""


def _render_account(account: dict[str, Any]) -> str:
    eq = float(account.get("equity", 0))
    avail = float(account.get("available", 0))
    notional = float(account.get("notional", 0))
    npos = int(account.get("position_count", 0))
    day_pnl = float(account.get("day_pnl", 0))
    return (f"净值${eq:.2f} 可用${avail:.2f} 已用名义${notional:.1f} "
            f"持仓{npos}个 今日盈亏${day_pnl:+.2f}")


def build_open_prompt(inp: "AIInput") -> str:
    lines = [
        f"标的: {inp.symbol}  当前价${inp.quote.get('lastPr', '-')} "
        f"(24h {float(inp.quote.get('changeUtc24h', 0) or 0) * 100:+.2f}%)  时段: {inp.session}",
        f"账户: {_render_account(inp.account)}",
    ]
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
    lines.append(inp.ind_4h_line)
    lines.append(inp.ind_1h_line)
    lines.append(inp.ind_5m_line)
    lines.append(inp.ind_1d_line)
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
                 "① 4H方向是向上吗(只在趋势向上或即将反转向上时考虑)? "
                 "② 现在的位置是买点吗(回踩企稳/突破中继, 而非高位追涨)? "
                 "③ 止损放哪(1%~12%), 止盈对应盈亏比是否≥1.5(扣手续费后仍赚)? "
                 "④ 该股符合'超市进货'标准吗——价格合理、有上涨动能、题材/新闻支持?")
    lines.append("判断完毕直接输出JSON。")
    return "\n".join(lines)


SYSTEM_MANAGE = """你是持仓管理AI(超市店长)。为每个持仓独立决策, 只做多。

原则:
1. 盈利单: 浮盈较小(0.5%~2%)且趋势仍向上 → 可继续持有(HOLD), 也可上调止盈保护利润;
   浮盈已够(≥1.5%)或动能减弱 → 兑现(CLOSE)。
2. 亏损单: 亏损<2%且趋势/形态未坏 → 拿住等回涨(HOLD), 超市理论: 没买在高位的正常股票
   一段时间会涨回来; 亏损<2%但明显买错(追高被套/跌破支撑) → 认错(CLOSE)小亏离场。
   亏损≥3% → 无论什么理由都要认真考虑止损(CLOSE), 别让亏损单拖垮账户。
3. 止损条件(任一出现立即CLOSE): 4H趋势明确转坏 / 跌破近期关键低点且放量 /
   突发重大利空 / 亏损已超过你愿意接受的范围。
4. 止盈止损可以随行情调整(ADJUST): 移动止损保本、上移止盈位。但别频繁微调。
5. 全仓模式下你不需要担心单仓爆仓, 但账户整体净值在下降时要果断收缩。

输出格式(严格JSON, 直接给结果):
{"action":"HOLD"或"ADJUST"或"CLOSE","stop_loss":止损价或null,"take_profit":止盈价或null,"reason":"50字内中文理由"}
HOLD=保持现状或给出期望的新价位(会在差异超过0.2%时执行), CLOSE=立即平仓。"""


def build_manage_prompt(inp: "AIInput", pos: dict[str, Any]) -> str:
    pnl_pct = float(pos.get("unrealized_pnl_pct", 0))
    entry = float(pos.get("avg_entry", 0))
    last = float(inp.quote.get("lastPr", 0))
    lines = [
        f"持仓: {inp.symbol}  开仓价${entry:.2f}  现价${last:.2f}  "
        f"浮盈亏{pnl_pct:+.2f}%  名义${float(pos.get('notional', 0)):.1f}",
        f"账户: {_render_account(inp.account)}",
    ]
    if inp.funding:
        lines.append(f"资金费率: {inp.funding}")
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
    lines.append("决策步骤(内心完成): ① 方向还对吗(4H/1H) ② 位置止损清晰吗 ③ 盈利兑现还是拿住 "
                 "④ 亏损是'正常回调可等回涨'还是'买错要认错' ⑤ 资金费率/时间成本")
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