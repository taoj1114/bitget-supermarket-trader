"""技术指标与形态计算(纯计算, 无网络)。

设计原则(来自 ai-native-trading skill 最佳实践):
- 精简指标集: 5m(日内) 7项 + 1H(趋势过滤) 3项 + 4H(定方向) + 1D(背景)
- trend_shape: 近2小时K线轨迹文本(形态/最近3根K线/波动范围/关键位)
- reversal_kline: 用户核心信号的程序化识别(4H 顶部/底部反转K线+位置门控)
- 所有 "crossed / just-happened" 状态预计算成标签(金叉/死叉/发散)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


# ---------- K线 → DataFrame ----------
def klines_to_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Bitget klines 横排数组 → DataFrame。列: ts, open, high, low, close, volume。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for c in ("open", "high", "low", "close", "baseVolume", "usdtVolume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.rename(columns={"baseVolume": "volume", "usdtVolume": "quote_volume"})
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df


def _wilder_smooth(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


# ---------- 指标集合 ----------
@dataclass
class IndicatorSet:
    rsi: float = 0.0
    ma10: float = 0.0
    ma30: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0        # ATR / close * 100
    vwap: float = 0.0
    vol_ratio: float = 0.0      # 最后一根量 / 前20均量
    bb_pos: float = 0.5         # (close-bblo)/(bbhi-bblo) ∈ [0,1]
    bb_hi: float = 0.0
    bb_lo: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    macd_cross: str = ""        # 金叉/死叉/多头发散/空头发散/-
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    regime: str = "flat"        # trend_up / trend_down / flat
    bias_ma5: float = 0.0       # (close/ma5-1)*100
    bias_ma10: float = 0.0
    bias_ma30: float = 0.0
    volume_status: str = ""     # 缩量回调/放量下跌/放量上涨/缩量上涨/-
    change_pct: float = 0.0     # 首根→末根的累计涨跌%


def _regime(adx: float, close: float, ma30: float, adx_th: float = 25.0) -> str:
    if adx >= adx_th:
        return "trend_up" if close > ma30 else "trend_down"
    return "flat"


def _volume_status(df: pd.DataFrame) -> str:
    if len(df) < 25:
        return "-"
    last_vol = df["volume"].iloc[-1]
    avg = df["volume"].iloc[-21:-1].mean()
    if avg <= 0:
        return "-"
    ratio = last_vol / avg
    close = df["close"].iloc[-1]
    prev = df["close"].iloc[-2]
    up = close > prev
    # 2026-09-19: 清洗价值判断标签(外部评审指出: 输入端的"洗盘/乏力/风险"标签
    # 与提示词"禁止用这些词当理由"自相矛盾, 且会带偏注意力) → 只保留中性量价事实
    if up and ratio >= 1.3:
        return "放量上涨"
    if up and ratio < 0.8:
        return "缩量上涨"
    if not up and ratio >= 1.3:
        return "放量下跌"
    if not up and ratio < 0.8:
        return "缩量回调"
    return "量平"


def compute_indicators(df: pd.DataFrame, primary: bool = True) -> IndicatorSet:
    """核心指标计算。primary=True 时额外算量价/乖离等。"""
    out = IndicatorSet()
    if df is None or len(df) < 35:
        return out
    close = df["close"]
    high, low = df["high"], df["low"]
    ts = df["ts"] if "ts" in df.columns else pd.Series(dtype=float)
    last = float(close.iloc[-1])

    # RSI(14, Wilder)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    last_rs = rs.iloc[-1]
    if pd.isna(last_rs):
        # 无亏损(全涨)或无盈利(全跌)
        out.rsi = 100.0 if gain.iloc[-1] > 0 else (0.0 if loss.iloc[-1] > 0 else 50.0)
    else:
        out.rsi = float(100 - 100 / (1 + last_rs))

    out.ma10 = float(close.rolling(10).mean().iloc[-1])
    out.ma30 = float(close.rolling(30).mean().iloc[-1])

    # ATR(14, Wilder)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    out.atr = float(_wilder_smooth(tr, 14).iloc[-1])
    out.atr_pct = float(out.atr / last * 100) if last else 0.0

    # VWAP 语义: 近20周期成交均价锚(原50根在下行周期过长, 误导'当日VWAP')
    n = min(20, len(df))
    tp = (high + low + close) / 3
    vw = (tp * df["volume"]).rolling(n).sum() / df["volume"].rolling(n).sum().replace(0, np.nan)
    out.vwap = float(vw.iloc[-1]) if not np.isnan(vw.iloc[-1]) else last

    # 量比: 用「最后一根完整K线」而非可能未完成的当前K线(避免半根K线量被当整量)
    v = df["volume"]
    last_idx = len(df) - 1
    if len(df) >= 2:
        period_ms = float(ts.iloc[-1] - ts.iloc[-2]) if len(ts) >= 2 else 0.0
        if period_ms > 0 and float(ts.iloc[-1]) + period_ms > time.time() * 1000:
            last_idx = len(df) - 2  # 当前K线未完成, 用上一根
    mean20 = v.iloc[max(0, last_idx - 20):last_idx].mean()
    out.vol_ratio = float(v.iloc[last_idx] / max(mean20, 1e-9))

    # 布林带(20, 2σ)
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std()
    bb_up = mid + 2 * sd
    bb_lo = mid - 2 * sd
    out.bb_hi = float(bb_up.iloc[-1])
    out.bb_lo = float(bb_lo.iloc[-1])
    span = (out.bb_hi - out.bb_lo) or 1e-9
    out.bb_pos = float(np.clip((last - out.bb_lo) / span, 0, 1))

    # MACD(12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out.macd = float(ema12.iloc[-1] - ema26.iloc[-1])
    macd_line = ema12 - ema26
    sig = macd_line.ewm(span=9, adjust=False).mean()
    out.macd_signal = float(sig.iloc[-1])
    out.macd_hist = float(out.macd - out.macd_signal)
    h0, h1 = out.macd_hist, float((macd_line - sig).iloc[-2]) if len(df) > 1 else 0.0
    if h1 < 0 <= h0:
        out.macd_cross = "金叉" if out.macd > 0 else "金叉(零轴下)"
    elif h1 > 0 >= h0:
        out.macd_cross = "死叉" if out.macd < 0 else "死叉(零轴上)"
    elif h0 > 0:
        out.macd_cross = "多头发散"
    elif h0 < 0:
        out.macd_cross = "空头发散"
    else:
        out.macd_cross = "-"

    # ADX(14, Wilder)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_s = _wilder_smooth(tr, 14).replace(0, np.nan)
    plus_di = 100 * _wilder_smooth(pd.Series(plus_dm, index=df.index), 14) / atr_s
    minus_di = 100 * _wilder_smooth(pd.Series(minus_dm, index=df.index), 14) / atr_s
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    out.adx = float(_wilder_smooth(dx.dropna(), 14).iloc[-1]) if not dx.dropna().empty else 0.0
    out.plus_di = float(plus_di.iloc[-1]) if not np.isnan(plus_di.iloc[-1]) else 0.0
    out.minus_di = float(minus_di.iloc[-1]) if not np.isnan(minus_di.iloc[-1]) else 0.0
    out.regime = _regime(out.adx, last, out.ma30)
    out.change_pct = float((last / close.iloc[0] - 1) * 100) if close.iloc[0] else 0.0

    if primary:
        ma5 = float(close.rolling(5).mean().iloc[-1]) if len(df) >= 5 else last
        out.bias_ma5 = float((last / ma5 - 1) * 100)
        out.bias_ma10 = float((last / out.ma10 - 1) * 100)
        out.bias_ma30 = float((last / out.ma30 - 1) * 100)
        out.volume_status = _volume_status(df)
    return out


# ---------- 趋势形态(近2小时 5m 轨迹) ----------
def trend_shape(df_5m: pd.DataFrame, lookback: int = 24) -> str:
    """把近 lookback 根 5m K线压缩成一行中文(形态+最近3根+波动+关键位)。"""
    if df_5m is None or len(df_5m) < 5:
        return "数据不足"
    d = df_5m.tail(lookback).reset_index(drop=True)
    first = float(d["close"].iloc[0])
    last = float(d["close"].iloc[-1])
    total = (last / first - 1) * 100 if first else 0.0
    half = len(d) // 2
    seg1 = (float(d["close"].iloc[half - 1]) / first - 1) * 100 if half else 0.0
    seg2 = (last / float(d["close"].iloc[half - 1]) - 1) * 100 if half else 0.0

    if abs(total) < 0.3:
        shape = "横盘震荡"
    elif seg1 * seg2 < 0:
        shape = "反转回落" if seg1 > 0 else "反转反弹"
    elif abs(seg2) > 1.3 * abs(seg1) + 0.1:
        shape = "加速上涨" if seg2 > 0 else "加速下跌"
    elif abs(seg2) < 0.5 * abs(seg1):
        shape = "动量衰竭(涨势趋缓)" if seg1 > 0 else "动量衰竭(跌势趋缓)"
    else:
        shape = "单边上涨" if total > 0 else "单边下跌"

    candles = []
    for i in d.tail(3).itertuples():
        body = abs(i.close - i.open)
        rng = (i.high - i.low) or 1e-9
        upper = (i.high - max(i.open, i.close)) / rng
        lower = (min(i.open, i.close) - i.low) / rng
        if body / rng < 0.3:
            kind = "十字" if upper < 0.35 and lower < 0.35 else ("阳长上影" if i.close >= i.open else "阴长下影")
        else:
            kind = "阳" if i.close >= i.open else "阴"
            if upper > 0.5: kind += "长上影"
            if lower > 0.5: kind += "长下影"
        candles.append(kind)

    hi = float(d["high"].max()); lo = float(d["low"].min())
    rng_pct = (hi - lo) / lo * 100 if lo else 0.0
    hi_d = (last / hi - 1) * 100; lo_d = (last / lo - 1) * 100
    return (f"{shape} 近3根[{'>'.join(candles)}] 区间{total:+.2f}% "
            f"范围{rng_pct:.2f}% 关键位: 高{hi:.2f}(距{hi_d:+.2f}%) 低{lo:.2f}(距{lo_d:+.2f}%)")


# ---------- 反转K线(用户核心信号) ----------
def reversal_kline(df_4h: pd.DataFrame, adx_1h: float = 0.0) -> str:
    """4H 顶部/底部反转K线识别(带位置门控)。返回 ⚠️ 信号文本或 ""。

    用户定义: K线走到一定位置时, 交易量与K线不成正比, 四小时开盘收盘接近,
    但最高/开盘、最低/收盘波动大 → 行情反转。
    """
    if df_4h is None or len(df_4h) < 15:
        return ""
    window = df_4h.tail(11).reset_index(drop=True)  # 1根候选 + 10根窗口
    last_c = window.iloc[-1]
    win_hi = float(window["high"].max())
    win_lo = float(window["low"].min())

    # 前段涨跌(候选前 5 根)
    prior = df_4h.tail(16).iloc[:-1]
    if len(prior) < 5:
        return ""
    p0 = float(prior["close"].iloc[-6]) if len(prior) >= 6 else float(prior["close"].iloc[0])
    trend = (float(prior["close"].iloc[-1]) / p0 - 1) * 100

    o, h, l, c = (float(last_c[k]) for k in ("open", "high", "low", "close"))
    rng = (h - l) or 1e-9
    body_ratio = abs(c - o) / rng
    upper_wick = (h - max(o, c)) / rng
    lower_wick = (min(o, c) - l) / rng

    pos_pct = (c - win_lo) / (win_hi - win_lo) * 100 if win_hi > win_lo else 50

    # 量价: 候选量 vs 前10均量
    v = float(window["volume"].iloc[-1])
    vavg = float(window["volume"].iloc[:-1].mean()) or 1e-9
    vratio = v / vavg

    signs: list[str] = []
    # 顶部反转: 前段涨 + 小实体 + 长上影 + (放量 or 缩量滞涨) + 高位
    high_pos = pos_pct > 85 or (win_hi > 0 and c >= win_hi * 0.97)
    if trend > 1.5 and body_ratio < 0.35 and upper_wick > 0.5 and high_pos:
        if vratio > 1.5:
            signs.append(f"放量长上影(量比{vratio:.1f}=抛压)")
        elif vratio < 0.7:
            signs.append(f"缩量滞涨(量比{vratio:.1f}=买盘枯竭)")
    # 底部反转: 前段跌 + 小实体 + 长下影 + 放量 + 低位
    low_pos = pos_pct < 15 or (win_lo > 0 and c <= win_lo * 1.03)
    if trend < -1.5 and body_ratio < 0.35 and lower_wick > 0.5 and low_pos:
        if vratio > 1.3:
            signs.append(f"放量长下影(量比{vratio:.1f}=承接)")

    if not signs:
        # 4H 顶部破位结构: 窗口内有暴涨后放量出货+回落
        if pos_pct < 35 and len(window) >= 6:
            peak_i = int(window["high"].idxmax()) if window["high"].idxmax() == window["high"].idxmax() else -1
            if peak_i >= 0:
                peak = float(window["high"].iloc[peak_i])
                pull = (c / peak - 1) * 100
                vol_after = float(window["volume"].iloc[peak_i:].max())
                if pull < -4 and peak_i <= len(window) - 3 and vol_after > 1.5 * float(window["volume"].iloc[peak_i]):
                    signs.append(f"放量出货后回落{pull:.1f}%(禁做多)")
    if not signs:
        return ""
    side = "顶部" if (signs and trend > 1.5) else "底部"
    pos_label = f"高位{pos_pct:.0f}%" if side == "顶部" else f"低位{pos_pct:.0f}%"
    return f"⚠️4H{side}反转K线({pos_label}): 前段{trend:+.1f}% 小实体 长影线 {','.join(signs)}"


# ---------- 关键位 ----------
def key_levels(df_4h: pd.DataFrame, n: int = 12) -> tuple[float, float]:
    if df_4h is None or len(df_4h) < n:
        return 0.0, 0.0
    w = df_4h.tail(n)
    return float(w["high"].max()), float(w["low"].min())


# ---------- 深跌反转(用户场景: 跌40-50%后开始反转) ----------
def deep_dip_reversal(df_1d: pd.DataFrame) -> tuple[bool, str]:
    """识别"深跌后企稳反转"的标的(用户命题 2026-09 数据验证):

    研究(44标的×2271事件): 已回撤≥40%的时点, 未来60日继续最大跌幅中位-4.6%,
    83%概率不再亏超15%, 但继续跌≥25%仍有7%尾部 → 必须企稳信号确认, 不接飞刀。

    判定(全部满足才 True):
    1. 距近120日高点回撤 ≥35%(深跌)
    2. 收盘站上 MA5(短线企稳)
    3. 自近20日低点反弹 ≥5%(反转迹象)
    """
    if df_1d is None or len(df_1d) < 40:
        return False, ""
    close = df_1d["close"]
    last = float(close.iloc[-1])
    hi120 = float(close.rolling(120, min_periods=40).max().iloc[-1])
    if hi120 <= 0:
        return False, ""
    dd = last / hi120 - 1
    lo20 = float(close.rolling(20, min_periods=10).min().iloc[-1])
    ma5 = float(close.rolling(5).mean().iloc[-1])
    bounce = last / lo20 - 1 if lo20 > 0 else 0.0
    if dd <= -0.35 and last >= ma5 and bounce >= 0.05:
        return True, (f"深跌反转: 距高点{dd * 100:.0f}% 自20日低点反弹{bounce * 100:.0f}% "
                      f"站上MA5 — 83%概率不再亏超15%, 但7%尾部须止损纪律")
    return False, ""


# ---------- 一句话渲染 ----------
def render_ind(ind: IndicatorSet, label: str, extra: str = "") -> str:
    """输出形如: 5m RSI 52.1 MA10 220.5 MA30 219.8 ATR 0.9% VWAP 220.3 量比1.2 BB0.45 ..."""
    # 2026-09-19(用户: 给实在OHLC而非指标): 移除 量比/BB/MACD —— 实证无预测力
    # (量比全桶58-60%, MACD多空头标签无区分度), 端侧少给误导, token让给OHLC数组。
    parts = [
        f"RSI{ind.rsi:.1f}", f"MA10 {ind.ma10:.2f}", f"MA30 {ind.ma30:.2f}",
        f"ATR {ind.atr_pct:.2f}%", f"20期均价 {ind.vwap:.2f}",
    ]
    if ind.adx > 0:
        parts.append(f"ADX{ind.adx:.1f}")
    if ind.regime != "flat":
        parts.append(f"regime={ind.regime}")
    if label.startswith(("4H", "日线")) or "5m" in label:
        parts.append(f"乖离MA30 {ind.bias_ma30:+.1f}%")
    if ind.volume_status and ind.volume_status != "-":
        parts.append(ind.volume_status)
    if extra:
        parts.append(extra)
    return f"{label} " + " ".join(parts)

def reversal_short(df: pd.DataFrame, level: str = "15m") -> tuple[str, str]:
    """短周期反转确认(2026-09-25 用户: 反转后顺势 —— 买在反转启动位置, 不追趋势中段)。

    在"连续下行/上行"之后, 检测最近已收盘 1-2 根 K 线的反转形态:
      底部反转: 长下影阳线(下影≥实体2倍) / 收盘收复前2根高点 / 放量长阳吞没前根阴线
                —— 且不再创新低(近3根低点 ≥ 更早3根最低点)
      顶部反转: 长上影阴线 / 收盘跌破前2根低点 / 放量长阴吞没前根阳线
                —— 且不再创新高
    返回 ("bottom"/"top"/"", 中文依据描述)。纯价格/成交量, 无指标。
    """
    try:
        if df is None or len(df) < 8:
            return ("", "")
        d = df.iloc[:-1] if len(df) > 6 else df      # 用已收盘, 防进行中抖动
        o = [float(x) for x in d["open"].tolist()]
        h = [float(x) for x in d["high"].tolist()]
        l = [float(x) for x in d["low"].tolist()]
        c = [float(x) for x in d["close"].tolist()]
        v = [float(x) for x in d.get("volume", pd.Series([0.0] * len(c))).tolist()]
        if len(c) < 8:
            return ("", "")
        # 前置趋势: 前 6 根(不含最近2根)是否连续下行/上行
        pre_h, pre_l = h[-8:-2], l[-8:-2]
        down_seq = sum(1 for i in range(1, len(pre_l)) if pre_l[i] <= pre_l[i - 1]) >= len(pre_l) - 3
        up_seq = sum(1 for i in range(1, len(pre_h)) if pre_h[i] >= pre_h[i - 1]) >= len(pre_h) - 3
        body = abs(c[-1] - o[-1])
        rng = max(h[-1] - l[-1], 1e-9)
        lower_wick = (min(o[-1], c[-1]) - l[-1]) / rng
        upper_wick = (h[-1] - max(o[-1], c[-1])) / rng
        avg_v = sum(v[-6:-1]) / max(1, len(v[-6:-1])) if len(v) >= 6 else 0.0
        vol_up = avg_v > 0 and v[-1] >= avg_v * 1.15
        no_new_low = min(l[-3:]) >= min(l[-6:-3]) * 0.999
        no_new_high = max(h[-3:]) <= max(h[-6:-3]) * 1.001
        # 去噪(2026-09-29 实测 MSFT 案例: 单根 15m 破位后立即收复 = 假信号, AI 正确拒绝):
        #   ①破位类信号(收复前高/跌破前低/吞没)必须**放量**(否则是区间内噪声);
        #   ②信号K线之后若已被反向收复(最新一根收盘越过信号K线端点), 视为失效。
        def _reclaimed_up() -> bool:
            """最新一根是否已收复信号(向上收复 = 顶部信号失效)"""
            return c[-1] > max(o[-1], c[-1]) * 1.0 and c[-1] > h[-2] * 1.0

        def _reclaimed_down() -> bool:
            return c[-1] < min(o[-1], c[-1]) * 1.0 and c[-1] < l[-2] * 1.0

        # 底部反转
        if down_seq and no_new_low:
            if c[-1] > o[-1] and lower_wick >= 0.45 and (vol_up or lower_wick >= 0.6):
                return ("bottom", f"{level}长下影阳线(下影{lower_wick:.0%}, 量{v[-1]/max(avg_v,1e-9):.1f}倍) + 不再创新低")
            if c[-1] > max(h[-3:-1]) and vol_up:
                return ("bottom", f"{level}放量收复前2根高点(量{v[-1]/max(avg_v,1e-9):.1f}倍) + 不再创新低")
            if c[-1] > o[-1] and c[-1] > o[-2] and vol_up and body > 0:
                return ("bottom", f"{level}放量长阳吞没前根阴线(量{v[-1]/max(avg_v,1e-9):.1f}倍)")
        # 顶部反转
        if up_seq and no_new_high:
            if c[-1] < o[-1] and upper_wick >= 0.45 and (vol_up or upper_wick >= 0.6):
                return ("top", f"{level}长上影阴线(上影{upper_wick:.0%}, 量{v[-1]/max(avg_v,1e-9):.1f}倍) + 不再创新高")
            if c[-1] < min(l[-3:-1]) and vol_up and not _reclaimed_down():
                return ("top", f"{level}放量跌破前2根低点(量{v[-1]/max(avg_v,1e-9):.1f}倍) + 不再创新高")
            if c[-1] < o[-1] and c[-1] < o[-2] and vol_up and body > 0:
                return ("top", f"{level}放量长阴吞没前根阳线(量{v[-1]/max(avg_v,1e-9):.1f}倍)")
        return ("", "")
    except Exception:
        return ("", "")
