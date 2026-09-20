"""用户 2026-09-19: 给 AI 实实在在的 OHLC 原始数组(开/高/低/收), 而非只给二手指标。"""

import pandas as pd

from supermarket.market import fmt_ohlc, MarketData


def test_fmt_ohlc_basic():
    df = pd.DataFrame({"open": [10.0, 10.5, 11.0], "high": [10.8, 11.2, 11.5],
                       "low": [9.9, 10.4, 10.8], "close": [10.5, 11.0, 11.2]})
    assert fmt_ohlc(df, 60) == "10.0/10.8/9.9/10.5, 10.5/11.2/10.4/11.0, 11.0/11.5/10.8/11.2"


def test_fmt_ohlc_tail_and_empty():
    df = pd.DataFrame({"open": [1.0, 2.0, 3.0, 4.0], "high": [1.5, 2.5, 3.5, 4.5],
                       "low": [0.5, 1.5, 2.5, 3.5], "close": [1.2, 2.2, 3.2, 4.2]})
    assert len(fmt_ohlc(df, 2).split(", ")) == 2          # 只取最近 n 根
    assert fmt_ohlc(pd.DataFrame()) == ""                  # 空 df 安全
    assert fmt_ohlc(None) == ""


def test_ohlc_in_open_prompt_and_lean_indicators():
    """集成: 真实 build_input → prompt 含三档 OHLC; 指标行不再含量比/MACD/BB。"""
    import sys
    from supermarket.config import Config
    from supermarket.bitget_client import BitgetClient
    from supermarket.prompts import build_open_prompt
    c = Config.load(); bg = BitgetClient(**c.bitget.__dict__); m = MarketData(bg, cfg=c)
    q = bg.quote("TSLAUSDT")
    inp = m.build_input("TSLAUSDT", q, {"equity": 55.0})
    pr = build_open_prompt(inp)
    assert "日线OHLC(已收盘, 近60根" in pr
    assert "4H OHLC(近30根" in pr
    assert "1H OHLC(近20根" in pr
    # 60+30+20 根数组都在
    import re
    m1 = re.search(r"日线OHLC\(已收盘, 近60根[^)]*\): (.*)", pr)
    assert m1 and len(m1.group(1).split(", ")) >= 55, "日线OHLC根数不足"
    # 指标行瘦身: 无量比/MACD/BB
    for ln in (inp.ind_1d_line, inp.ind_4h_line, inp.ind_1h_line, inp.ind_5m_line):
        assert "量比" not in ln and "MACD" not in ln and "BB" not in ln, ln[:80]
