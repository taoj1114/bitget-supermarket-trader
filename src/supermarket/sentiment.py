"""市场情绪评分(2026-09 用户提议)。

设计原则:
- 只做"事实输入"给 AI(不是程序决策), AI 自行判断如何使用
- 数据全部可溯源: 池内宽度(本地 tickers, 零额外请求) + VIX(Yahoo Finance, 1h 缓存)
- 任何环节失败都不阻塞交易(降级为无情绪数据)

评分 0-100: 0=极度恐慌, 50=中性, 100=极度贪婪。
权重: 池内宽度 35% + 中位涨幅 20% + 极端分布 15% + VIX 30%(VIX 不可用时前三项归一化)。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

log = logging.getLogger(__name__)

_VIX_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
            "?interval=1d&range=5d")
_vix_cache: tuple[float, float | None] = (0.0, None)


def fetch_vix(ttl: float = 3600.0) -> float | None:
    """取 VIX 现值。1 小时缓存; 失败返回 None(绝不抛异常打断主流程)。"""
    global _vix_cache
    ts, val = _vix_cache
    if val is not None and time.time() - ts < ttl:
        return val
    try:
        req = urllib.request.Request(_VIX_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        v = float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        if v > 0:
            _vix_cache = (time.time(), v)
            return v
    except Exception as e:
        log.debug("VIX 获取失败(不影响交易): %s", str(e)[:80])
    return None


def vix_score(vix: float) -> float:
    """VIX → 情绪分: 低 VIX(平静)= 偏贪婪, 高 VIX(恐慌)= 偏恐惧。"""
    if vix <= 12:
        return 90.0
    if vix <= 15:
        return 80.0
    if vix <= 18:
        return 70.0
    if vix <= 22:
        return 55.0
    if vix <= 28:
        return 35.0
    if vix <= 35:
        return 20.0
    return 8.0


def label_of(score: float) -> str:
    if score >= 75:
        return "极度贪婪"
    if score >= 60:
        return "贪婪"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "恐惧"
    return "极度恐慌"


def compute_sentiment(changes: list[float], vix: float | None = None) -> dict:
    """合成情绪评分。

    changes: 美股池各标的 24h 涨跌幅(%)
    vix:     VIX 现值(None = 该维度缺失, 权重归一化)
    """
    n = len(changes)
    if n == 0:
        return {"score": 50.0, "label": "数据不足", "n": 0, "vix": vix}
    srt = sorted(changes)
    up_pct = sum(1 for x in srt if x > 0) / n * 100
    med = srt[n // 2]
    panic_pct = sum(1 for x in srt if x <= -3) / n * 100
    greed_pct = sum(1 for x in srt if x >= 3) / n * 100

    s_breadth = up_pct                                  # 0-100 直接
    s_med = max(0.0, min(100.0, 50.0 + med / 3.0 * 50.0))  # -3%→0, 0→50, +3%→100
    s_ext = max(0.0, min(100.0, 50.0 + (greed_pct - panic_pct) * 2.5))

    parts = [(s_breadth, 0.35), (s_med, 0.20), (s_ext, 0.15)]
    sv = vix_score(vix) if vix else None
    if sv is not None:
        parts.append((sv, 0.30))
    wsum = sum(w for _, w in parts)
    score = round(sum(s * w for s, w in parts) / wsum, 1)

    return {
        "score": score,
        "label": label_of(score),
        "n": n,
        "up_pct": round(up_pct, 1),
        "median_chg": round(med, 2),
        "panic_pct": round(panic_pct, 1),
        "greed_pct": round(greed_pct, 1),
        "vix": vix,
        "vix_score": round(sv, 1) if sv is not None else None,
    }


def format_line(s: dict) -> str:
    """渲染成给 AI 的一行事实描述。"""
    if not s.get("n"):
        return ""
    base = (f"市场情绪 {s['score']:.0f}/100 {s['label']} | "
            f"池内上涨{s.get('up_pct', 0):.0f}% 中位{s.get('median_chg', 0):+.2f}% "
            f"(暴跌{s.get('panic_pct', 0):.0f}% 大涨{s.get('greed_pct', 0):.0f}%)")
    if s.get("vix"):
        base += f" | VIX {s['vix']:.1f}"
    return base
