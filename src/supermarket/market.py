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

import pandas as pd

from supermarket.indicators import (
    compute_indicators,
    deep_dip_reversal,
    key_levels,
    klines_to_df,
    render_ind,
    reversal_kline,
    trend_shape,
)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

GRANULARITIES = {"5m": "5m", "1H": "1H", "4H": "4H", "1D": "1D", "1W": "1W"}
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
        # 2026-09-19: 同样去价值判断 — 只给买卖量比数值, 由 AI 自行解读
        return (f"盘口买卖量比{ratio:.2f}(买{bid_vol:.0f}/卖{ask_vol:.0f}) "
                f"价差{spread:.3f}% 首档大单{big:.0f}")
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
    deep_dip: str = ""           # 深跌反转信号(深跌40%+且企稳)或 ""
    rs20: float = 0.0            # 相对强度: 个股20日收益 - SPY20日收益(%)
    data_ok: bool = True         # 日线数据是否足够(不足则跳过决策, 不浪费AI调用/防误判)
    new_high_5d: bool = False    # 近5日是否创过新高(趋势是否仍在推进; 实证关键区分)
    days_since_high: int = 0     # 距上次创(20日)新高的天数
    momentum_state: str = ""     # 动量状态: 推进中/滞涨/乏力(浮盈持仓的卖出信号, 实证核心)
    pos20: float = 0.0           # 距20日最高(%)
    current_holding: str = ""    # 已持仓信息(加仓决策用)
    market_env: str = ""         # 大盘环境(SPY/QQQ, 只读参考)
    daily_levels: str = ""       # 日线关键位(20日高低/MA30, 结构止损参考)
    weekly_line: str = ""        # 周线季节视角(仅13根, 季度方向参考)


class MarketData:
    def __init__(self, bg, kline_ttl: float = 60.0, cfg: Config | None = None):
        self.bg = bg
        self.kline_ttl = kline_ttl
        self.cfg = cfg or Config()
        self._cache: dict[tuple[str, str, int], tuple[float, list]] = {}
        self._ticker_cache: tuple[float, list] = (0.0, [])

    def klines(self, symbol: str, gf: str, limit: int = KLINE_LIMIT):
        key = (symbol, gf, limit)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.kline_ttl:
            return hit[1]
        # Bitget 只返回最近 ~100-200 根; granularity 枚举实测 '5m'/'1H'/'4H'/'1D'
        rows = self.bg.klines(symbol, GRANULARITIES[gf], min(limit, 1000))
        self._cache[key] = (now, rows)
        return rows

    def tickers(self) -> list[dict[str, Any]]:
        now = time.time()
        if now - self._ticker_cache[0] < 10:
            return self._ticker_cache[1]
        ticks = self.bg.tickers()
        self._ticker_cache = (now, ticks)
        return ticks

    def daily_regime(self, symbol: str = "SPYUSDT") -> tuple[str, float] | None:
        """轻量取日线 regime/adx(供大盘趋势门控用, 不构建完整 AI 输入)。"""
        try:
            df = klines_to_df(self.klines(symbol, "1D", 60))
            if df is None or len(df) < 30:
                return None
            if len(df) > 40:
                df = df.iloc[:-1]  # 只用已收盘日线(防盘中抖动); 数据不足则沿用防跌破最小长度
            ind = compute_indicators(df, primary=False)
            return ind.regime, float(ind.adx)
        except Exception as e:
            log.debug("daily_regime %s 失败: %s", symbol, str(e)[:60])
            return None

    def build_input(self, symbol: str, quote: dict[str, Any],
                    account: dict[str, Any], history: str = "",
                    lessons: list[str] | None = None,
                    manage: bool = False,
                    current_holding: str = "",
                    extra_env: str = "") -> AIInput:
        lessons = lessons or []
        lim = self.cfg.kline_limits
        df5 = klines_to_df(self.klines(symbol, "5m", lim.get("5m")))
        df1h = klines_to_df(self.klines(symbol, "1H", lim.get("1H")))
        df4h = klines_to_df(self.klines(symbol, "4H", lim.get("4H")))
        df1d = klines_to_df(self.klines(symbol, "1D", lim.get("1D")))
        # 日线方向权威必须基于"已收盘日线": 进行中的最后一根会让 regime/ADX 盘中抖动
        # (2026-09 实证: SPCX 盘中显示 trend_down → AI 平仓亏$0.33, 当日收盘实为 trend_up ADX27.4)
        # 仅在有足够余量时剔除(指标计算最小长度 35 根; 日线不足的标的直接沿用, 防全0)
        df1d_done = df1d.iloc[:-1] if len(df1d) > 40 else df1d
        # 1W 周线=季节增强, 失败降级为空(不阻塞扫描)
        df1w = pd.DataFrame()
        try:
            df1w = klines_to_df(self.klines(symbol, "1W", lim.get("1W", 13)))
        except Exception as e:
            log.debug("1W 拉取失败(降级为空): %s", str(e)[:60])

        ind5 = compute_indicators(df5, primary=True)
        ind1h = compute_indicators(df1h, primary=False)
        ind4h = compute_indicators(df4h, primary=True)
        ind1d = compute_indicators(df1d_done, primary=True)  # 日线方向权威=已收盘, 指标全量

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

        # 深跌反转信号(用户场景 2026-09): 连续跌40%+的热门票, 企稳后可以尝试买入
        # 数据支撑(44标的×2271事件): 深跌后83%概率不再亏超15%, 但7%尾部须止损纪律
        deep_dip = ""
        if not manage:
            ok_dd, dd_info = deep_dip_reversal(df1d_done)
            if ok_dd:
                deep_dip = dd_info

        news = ""

        # 大盘环境(只读参考, 不交易): SPY/QQQ 24h涨跌 + 日线趋势 + UTC时间
        market_env = ""
        try:
            spyq = self.bg.quote("SPYUSDT")
            qqqq = self.bg.quote("QQQUSDT")
            chg = lambda q: float(q.get("changeUtc24h") or 0) * 100
            # 直接调用门控同源方法(daily_regime), 保证 AI 看到的与程序门控完全一致
            _mr = self.daily_regime("SPYUSDT")
            _reg = _mr[0] if _mr else "flat"
            _adx = _mr[1] if _mr else 0.0
            market_env = (f"[{time.strftime('%m-%d %H:%M')} UTC] 大盘: "
                          f"SPY {chg(spyq):+.2f}% 24h(已收日线{_reg} ADX{_adx:.0f}) | "
                          f"QQQ {chg(qqqq):+.2f}% 24h")
        except Exception as e:
            log.debug("大盘环境获取失败: %s", str(e)[:60])

        # 日线关键位(结构止损参考): 20日高低 + MA30
        daily_levels = ""
        rs20 = 0.0
        pos20 = 0.0
        new_high_5d = False
        days_since_high = 0
        momentum_state = ""
        try:
            if len(df1d_done) >= 20:
                dhi = float(df1d_done["high"].tail(20).max())
                dlo = float(df1d_done["low"].tail(20).min())
                ma30 = ind1d.ma30
                last_px = float(quote.get("lastPr", 0) or 0)
                dev = (last_px / ma30 - 1) * 100 if ma30 and last_px else 0.0
                # 位置(距20日高) + 相对强度 vs SPY(2026-09 实证: RS<-10% fwd5胜率13.6%, 距高<-10% 胜率16%)
                pos20 = (last_px / dhi - 1) * 100 if dhi and last_px else 0.0
                try:
                    spy_df = klines_to_df(self.klines("SPYUSDT", "1D", 60))
                    if len(spy_df) > 30:
                        spy_df = spy_df.iloc[:-1]   # 与门控一致: 只用已收盘日线
                    if len(df1d) >= 21 and len(spy_df) >= 21:
                        stk20 = (float(df1d["close"].iloc[-1]) / float(df1d["close"].iloc[-21]) - 1) * 100
                        spy20 = (float(spy_df["close"].iloc[-1]) / float(spy_df["close"].iloc[-21]) - 1) * 100
                        rs20 = stk20 - spy20
                except Exception:
                    rs20 = 0.0
                # 趋势是否仍在推进(2026-09 实证 53722 样本: 近5日有新高→超额+0.45%; 无新高→仅+0.10%)
                hi_list = [float(x) for x in df1d_done["high"].tolist()]
                if len(hi_list) >= 11:
                    new_high_5d = max(hi_list[-5:]) >= max(hi_list[-10:-5])
                    for k in range(len(hi_list) - 1, max(len(hi_list) - 21, 0), -1):
                        if hi_list[k] >= max(hi_list[max(0, k - 20):k + 1]):
                            days_since_high = len(hi_list) - 1 - k
                            break
                    else:
                        days_since_high = 20
                # 动量状态(2026-09 实证 481475 样本): 乏力 → 浮盈回吐概率 66% vs 推进中 40%
                hs = [float(x) for x in df1d_done["high"].tolist()]
                cs2 = [float(x) for x in df1d_done["close"].tolist()]
                if len(cs2) >= 6:
                    hi3, hi6 = max(hs[-3:]), max(hs[-6:-3])
                    ma5d = sum(cs2[-5:]) / 5
                    red2 = cs2[-1] < cs2[-2] and cs2[-2] < cs2[-3]
                    if hi3 >= hi6 and cs2[-1] > ma5d and not red2:
                        momentum_state = "推进中"
                    elif hi3 < hi6 and cs2[-1] <= ma5d:
                        momentum_state = "乏力"
                    elif hi3 < hi6:
                        momentum_state = "滞涨"
                    else:
                        momentum_state = "混合"
                tag = ""
                if pos20 <= -10:
                    tag = " ⚠️深跌中段(实证: 未企稳接刀 fwd5胜率仅16%)"
                elif rs20 <= -10:
                    tag = " ⚠️大幅跑输大盘(实证: fwd5胜率仅13.6%)"
                elif not new_high_5d:
                    tag = (" ⚠️近5日无新高=趋势未推进(实证: 此类回调超额收益仅+0.1% vs "
                           "创新高者+0.45%; 除非有放量确认, 否则等创新高再进货)")
                daily_levels = (f"日线位(20日): 高{dhi:.2f} 低{dlo:.2f} "
                                f"MA30 {ma30:.2f}(偏离{dev:+.1f}%) 跌破{min(dlo, ma30):.2f}=结构破坏 | "
                                f"距20日高{pos20:+.1f}% | RS(vsSPY20日){rs20:+.1f}% | "
                                f"近5日创新高:{'是' if new_high_5d else f'否(距上次新高{days_since_high}天)'} | "
                                f"动量(近3日口径):{momentum_state}{tag}")
        except Exception as e:
            log.debug("日线关键位计算失败: %s", str(e)[:60])

        # 周线季节视角(Bitget仅保留13根, 但足以看季度方向: 5/10周均线 + 近4周涨跌)
        weekly_line = ""
        try:
            cw = df1w["close"].astype(float)
            if len(cw) >= 5:
                ma5 = float(cw.rolling(5, min_periods=5).mean().iloc[-1])
                ma10 = float(cw.rolling(10, min_periods=10).mean().iloc[-1]) if len(cw) >= 10 else float("nan")
                last_w = float(cw.iloc[-1])
                chg4w = (last_w / float(cw.iloc[-5]) - 1) * 100
                if not pd.isna(ma10):
                    wdir = "上升" if ma5 >= ma10 and last_w >= ma5 else ("下降" if ma5 <= ma10 and last_w <= ma5 else "震荡")
                else:
                    wdir = "上升" if last_w >= ma5 else "下降"
                weekly_line = (f"周线(季节视角,{len(cw)}根): 方向{wdir} "
                               f"MA5 {ma5:.2f}/MA10 {ma10:.2f} 最近4周{chg4w:+.1f}% "
                               f"(同比上升=中长期上涨周期中的票, 超市进货优先)")
        except Exception as e:
            log.debug("周线视角失败: %s", str(e)[:60])

        return AIInput(
            symbol=symbol,
            quote=quote,
            session=us_session(),
            ind_5m_line=render_ind(ind5, "5m(短线时机):"),
            ind_1h_line=render_ind(ind1h, "1H(趋势):"),
            ind_4h_line=render_ind(ind4h, "4H(中趋势):"),
            ind_1d_line=render_ind(ind1d, "日线(已收, 定方向!):"),
            trend=trend,
            orderbook=orderbook,
            funding=funding,
            news=news,
            account=account,
            history=history,
            lessons=lessons,
            daily_regime=ind1d.regime,
            daily_adx=ind1d.adx,
            deep_dip=deep_dip,
            current_holding=current_holding,
            market_env=(market_env + (" | " + extra_env) if extra_env else market_env),
            daily_levels=daily_levels,
            rs20=rs20,
            pos20=pos20,
            new_high_5d=new_high_5d,
            days_since_high=days_since_high,
            momentum_state=momentum_state,
            data_ok=(len(df1d_done) >= 35 and len(df4h) >= 20),
            weekly_line=weekly_line,
        )