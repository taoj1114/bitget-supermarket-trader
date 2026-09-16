"""市场情绪评分测试(纯函数, 不触网)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.sentiment import (
    compute_sentiment,
    format_line,
    label_of,
    vix_score,
)


def test_vix_score_monotonic():
    """VIX 越高 → 情绪分越低(单调)。"""
    xs = [10, 13, 16, 20, 25, 32, 45]
    ys = [vix_score(x) for x in xs]
    assert ys == sorted(ys, reverse=True)
    assert ys[0] > 85 and ys[-1] < 10


def test_label_thresholds():
    assert label_of(80) == "极度贪婪"
    assert label_of(65) == "贪婪"
    assert label_of(50) == "中性"
    assert label_of(35) == "恐惧"
    assert label_of(10) == "极度恐慌"


def test_bullish_pool_scores_high():
    """普涨行情(多数上涨+中位正+无暴跌+VIX低) → 高分。"""
    changes = [1.5] * 80 + [0.3] * 15 + [-0.8] * 5
    s = compute_sentiment(changes, vix=14.0)
    assert s["score"] > 65, s
    assert s["label"] in ("贪婪", "极度贪婪")


def test_bearish_pool_scores_low():
    """普跌+暴跌家数多+VIX高 → 低分。"""
    changes = [-2.5] * 70 + [-5.0] * 20 + [0.5] * 10
    s = compute_sentiment(changes, vix=32.0)
    assert s["score"] < 35, s
    assert s["label"] in ("恐惧", "极度恐慌")
    assert s["vix_score"] < 25


def test_works_without_vix():
    """VIX 缺失时仍能出分(权重重归一化, 不报错)。"""
    s = compute_sentiment([1.0] * 50 + [-1.0] * 50, vix=None)
    assert 0 <= s["score"] <= 100
    assert s["vix"] is None
    assert "VIX" not in format_line(s)


def test_empty_input_safe():
    s = compute_sentiment([], vix=None)
    assert s["score"] == 50.0 and s["n"] == 0
    assert format_line(s) == ""


def test_media_bias_respected():
    """中位涨幅对分数的影响应体现方向性。"""
    up = compute_sentiment([0.5] * 100, vix=18.0)
    down = compute_sentiment([-0.5] * 100, vix=18.0)
    assert up["score"] > down["score"]


def test_format_line_contains_facts():
    s = compute_sentiment([1.0] * 60 + [-1.0] * 40, vix=17.0)
    line = format_line(s)
    assert "市场情绪" in line and "池内上涨" in line and "VIX" in line
