"""市场数据组装: K线/指标/盘口/资金费率 → AIInput(供 prompt 渲染)。

会话判定用 America/New_York 时区(自动 DST)。K线有 60s 缓存。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from supermarket.indicators import (
    compute_indicators,
    key_levels,
    klines_to_df,
    render_ind,
    reversal_kline,
    trend_shape,
)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

GRANULARITIES = {"5m": "5m", "1H": "1H", "4H": "4H", "1D": "1D"}
KLINE_LIMIT = 200


def us_session(now: datetime | None = None) -> str:
    """美股时段: pre_market/regular/post_market/closed/weekend。"""
    now = now or datetime.now(_ET)
    if now.weekday() >= 5:
        return "weekend"
    t = now.hour * 60 + now.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "pre_market"
    if 9 * 60 + 30 <= t < 16 * 60:
        return "regular"
    if 16 * 60 <= t < 20 * 60:
        return "post_market"
    return "closed"


def orderbook_pressure(book: dict[str, Any]) -> str:
    """盘口压力: 买卖前10档量比 + 价差 + 大单。空 book → ""。"""
    try:
        bids = [(float(p), float(s)) for p, s in book.get("bids", [])[:10]]
        asks = [(float(p), float(s)) for p, s in book.get("asks", [])[:10]]
        if not bids or not asks:
            return ""
        bid_vol = sum(s for _, s in bids)
        ask_vol = sum(s for _, s in asks)
        ratio = bid_vol / ask_vol if ask_vol else 0
        spread = (asks[0][0] - bids[0][0]) / bids[0][0] * 100 if bids[0][0] else 0
        big = max(bids[0][1], asks[0][1]) if (bids and asks) else 0
        side = "买压" if ratio > 1.1 else ("卖压" if ratio < 0.9 else "均衡")
        return f"{side} 买{bid_vol:.0f}/卖{ask_vol:.0f} ({ratio:.2f}) 价差{spread:.3f}% 首档大单{big:.0f}"
    except Exception:
        return ""


def funding_line(funding_rate: float | None, funding_time: str = "") -> str:
    if funding_rate is None:
        return ""
    pct = float(funding_rate) * 100
    verdict = "(多头付费)" if pct > 0 else ("(多头收费)" if pct < 0 else "")
    return f"费率{pct:+.4f}%/8h {verdict}" + (f" 结算:{funding_time}" if funding_time else "")


@dataclass
class AIInput:
    symbol: str
    quote: dict[str, Any]
    session: str
    ind_5m_line: str
    ind_1h_line: str
    ind_4h_line: str
    ind_1d_line: str
    trend: str
    orderbook: str = ""
    funding: str = ""
    news: str = ""
    account: dict[str, Any] = field(default_factory=dict)
    history: str = ""
    lessons: list[str] = field(default_factory=list)
    daily_regime: str = "flat"   # 日线 regime(代码级方向门控用)
    daily_adx: float = 0.0


class MarketData:
    def __init__(self, bg, kline_ttl: float = 60.0):
        self.bg = bg
        self.kline_ttl = kline_ttl
        self._cache: dict[tuple[str, str], tuple[float, list]] = {}
        self._ticker_cache: tuple[float, list] = (0.0, [])

    def klines(self, symbol: str, gf: str, limit: int = KLINE_LIMIT):
        key = (symbol, gf)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.kline_ttl:
            return hit[1]
        # Bitget 只返回最近 ~100-200 根; granularity 枚举实测 '5m'/'1H'/'4H'/'1D'
        rows = self.bg.klines(symbol, GRANULARITIES[gf], min(limit, 200))
        self._cache[key] = (now, rows)
        return rows

    def tickers(self) -> list[dict[str, Any]]:
        now = time.time()
        if now - self._ticker_cache[0] < 10:
            return self._ticker_cache[1]
        ticks = self.bg.tickers()
        self._ticker_cache = (now, ticks)
        return ticks

    def build_input(self, symbol: str, quote: dict[str, Any],
                    account: dict[str, Any], history: str = "",
                    lessons: list[str] | None = None,
                    manage: bool = False) -> AIInput:
        lessons = lessons or []
        df5 = klines_to_df(self.klines(symbol, "5m"))
        df1h = klines_to_df(self.klines(symbol, "1H"))
        df4h = klines_to_df(self.klines(symbol, "4H"))
        df1d = klines_to_df(self.klines(symbol, "1D"))

        ind5 = compute_indicators(df5, primary=True)
        ind1h = compute_indicators(df1h, primary=False)
        ind4h = compute_indicators(df4h, primary=True)
        ind1d = compute_indicators(df1d, primary=True)  # 日线是方向权威, 指标全量

        trend = trend_shape(df5)
        if not manage:
            rev = reversal_kline(df4h, ind1h.adx)
            if rev:
                trend = rev + "\n" + trend

        orderbook = ""
        try:
            orderbook = orderbook_pressure(self.bg.orderbook(symbol))
        except Exception as e:
            log.debug("orderbook 失败 %s: %s", symbol, str(e)[:60])

        funding = ""
        try:
            fr = quote.get("fundingRate")
            if fr is not None and float(fr) != 0:
                funding = funding_line(float(fr))
        except (TypeError, ValueError):
            pass

        news = ""
        return AIInput(
            symbol=symbol,
            quote=quote,
            session=us_session(),
            ind_5m_line=render_ind(ind5, "5m(日内):"),
            ind_1h_line=render_ind(ind1h, "1H(趋势):"),
            ind_4h_line=render_ind(ind4h, "4H(中趋势):"),
            ind_1d_line=render_ind(ind1d, "日线(定方向!):"),
            trend=trend,
            orderbook=orderbook,
            funding=funding,
            news=news,
            account=account,
            history=history,
            lessons=lessons,
            daily_regime=ind1d.regime,
            daily_adx=ind1d.adx,
        )