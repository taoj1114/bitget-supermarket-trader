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
    d = parse_open_decision('{"action":"SELL","stop_loss":100,"take_profit":110,"reason":"x"}')
    assert d.action == "HOLD"  # 只做多: SELL 归一为 HOLD


def test_parse_empty():
    d = parse_open_decision("")
    assert d.action == "HOLD"


def test_parse_manage():
    m = parse_manage_decision('{"action":"CLOSE","reason":"4H趋势转坏"}')
    assert m.is_close
    m2 = parse_manage_decision('{"action":"ADJUST","stop_loss":201.0,"take_profit":null,"reason":"保本"}')
    assert m2.action == "ADJUST"
    assert m2.stop_loss == 201.0


def test_fallback_provider():
    p = FallbackHOLDProvider()
    d = p.decide_open("s", "p")
    assert d.action == "HOLD"
    m = p.decide_manage("s", "p")
    assert m.action == "HOLD"
    assert p.review("s", "p") == []


def test_opencode_review_parse():
    p = OpenCodeProvider("https://x/v1", "k", "m")
    lessons = p.review.__wrapped__ if hasattr(p.review, "__wrapped__") else None
    # 直接测内部解析路径: 手工构造响应
    from supermarket.ai import parse_open_decision as pod
    # review 用 _extract_json_block + json.loads, 我们测等价解析
    import json as _json
    from supermarket.ai import _extract_json_block
    block = _extract_json_block('{"lessons":["追高被套","止损太紧"]}')
    assert _json.loads(block)["lessons"] == ["追高被套", "止损太紧"]