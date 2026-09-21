"""AI 决策 JSON 解析容错测试。"""

import pytest

from supermarket.ai import (
    FallbackHOLDProvider,
    OpenCodeProvider,
    parse_manage_decision,
    parse_open_decision,
)


def test_parse_clean():
    d = parse_open_decision('{"action":"BUY","stop_loss":214.5,"take_profit":231.2,"reason":"趋势向上"}')
    assert d.is_buy
    assert d.stop_loss == 214.5
    assert d.take_profit == 231.2


def test_parse_codeblock():
    d = parse_open_decision('```json\n{"action":"HOLD","stop_loss":null,"take_profit":null,"reason":"等回踩"}\n```')
    assert d.action == "HOLD"


def test_parse_with_leading_text():
    d = parse_open_decision('根据分析结果如下: {"action":"BUY","stop_loss":100,"take_profit":110,"reason":"ok"} 完毕')
    assert d.is_buy


def test_parse_truncated():
    d = parse_open_decision('{"action":"BUY","stop_loss":100.5,"take_profit":110.2,"reason":"趋势')
    assert d.is_buy
    assert d.stop_loss == 100.5


def test_parse_garbage_action():
    d = parse_open_decision('{"action":"FOO","stop_loss":100,"take_profit":110,"reason":"x"}')
    assert d.action == "HOLD"  # 非法动作归一为 HOLD


def test_parse_empty():
    d = parse_open_decision("")
    assert d.action == "HOLD"


def test_parse_manage():
    m = parse_manage_decision('{"action":"CLOSE","reason":"4H趋势转坏"}')
    assert m.is_close
    m2 = parse_manage_decision('{"action":"ADJUST","stop_loss":201.0,"take_profit":null,"reason":"保本"}')
    assert m2.action == "ADJUST"
    assert m2.stop_loss == 201.0


def test_parse_sell():
    d = parse_open_decision('{"action":"SELL","stop_loss":230.0,"take_profit":210.0,"reason":"日线转空"}')
    assert d.is_short
    assert d.stop_loss == 230.0
    assert d.take_profit == 210.0


def test_fallback_provider():
    p = FallbackHOLDProvider()
    d = p.decide_open("s", "p")
    assert d.action == "HOLD"
    m = p.decide_manage("s", "p")
    assert m.action == "HOLD"


def test_extract_json_block_lessons():
    """JSON 块提取(复盘移除后仍保留的基础解析能力)。"""
    import json as _json
    from supermarket.ai import _extract_json_block
    block = _extract_json_block('{"lessons":["追高被套","止损太紧"]}')
    assert _json.loads(block)["lessons"] == ["追高被套", "止损太紧"]

def test_parse_leverage_field():
    """AI 决定杠杆(2026-09-19): 解析 leverage 字段, 缺失/异常为 None。"""
    d = parse_open_decision('{"action":"BUY","leverage":12,"stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d.leverage == 12
    d2 = parse_open_decision('{"action":"HOLD","stop_loss":null,"take_profit":null,"reason":"x"}')
    assert d2.leverage is None
    d3 = parse_open_decision('{"action":"BUY","leverage":"8","stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d3.leverage == 8, "字符串数字应可解析"
    d4 = parse_open_decision('{"action":"BUY","leverage":"abc","stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d4.leverage is None, "非法值应降级为 None(风控用默认值)"


def test_parse_margin_usd_field():
    """AI 决定仓位大小(2026-09-22): 解析 margin_usd, 缺失为 None。"""
    d = parse_open_decision('{"action":"BUY","leverage":12,"margin_usd":3.5,"stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d.margin_usd == 3.5
    d2 = parse_open_decision('{"action":"HOLD","stop_loss":null,"take_profit":null,"reason":"x"}')
    assert d2.margin_usd is None
    d3 = parse_open_decision('{"action":"BUY","leverage":10,"margin_usd":"2.5","stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d3.margin_usd == 2.5


def test_parse_max_positions_field():
    """AI 决定最大仓数(2026-09-22): 解析 max_positions, 缺失为 None。"""
    d = parse_open_decision('{"action":"BUY","leverage":12,"max_positions":4,"stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d.max_positions == 4
    d2 = parse_open_decision('{"action":"HOLD","stop_loss":null,"take_profit":null,"reason":"x"}')
    assert d2.max_positions is None


def test_parse_max_notional_mult_field():
    """AI 决定资金利用率(2026-09-22): 解析 max_notional_mult, 缺失为 None。"""
    d = parse_open_decision('{"action":"BUY","leverage":12,"max_positions":4,"max_notional_mult":5.5,"stop_loss":100.0,"take_profit":115.0,"reason":"x"}')
    assert d.max_notional_mult == 5.5
    d2 = parse_open_decision('{"action":"HOLD","stop_loss":null,"take_profit":null,"reason":"x"}')
    assert d2.max_notional_mult is None
