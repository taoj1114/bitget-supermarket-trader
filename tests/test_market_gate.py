"""大盘趋势门控 + 当日止损熔断收紧 测试(2026-09 用户实证后新增)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.config import Config


def test_market_down_adx_default():
    c = Config.load()
    assert c.market_down_adx == 25.0


def test_daily_drawdown_tightened():
    """当日亏损熔断从 30% 收紧到 5%(用户实证: 阴跌期反复认错会累积)。"""
    c = Config.load()
    assert c.max_daily_drawdown_pct == 5.0


def test_market_gate_blocks_long_only(tmp_path):
    """个股日线向下: 多单拦, 空单放行(空单是弱市对冲手段)。"""
    from supermarket.risk import RiskEngine

    c = Config.load()
    rm = RiskEngine(c, tmp_path)
    ok, reason = rm.validate_daily_direction("trend_down", 30.0, "long")
    assert not ok and "禁做多" in reason
    ok, _ = rm.validate_daily_direction("trend_down", 30.0, "short")
    assert ok


def test_market_down_flag_wiring():
    """engine 里应存在大盘门控标记与 SPY 日线检测(防误删)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "_market_down" in src and "daily_regime" in src
    assert "大盘日线向下" in src


def test_daily_regime_method_exists():
    """market.py 必须提供轻量日线 regime 方法(门控数据源)。"""
    from supermarket.market import MarketData
    assert hasattr(MarketData, "daily_regime")


def test_mid_downtrend_gate_wiring():
    """中段下跌禁区(实证: 距20日高≤-10%且日线非向上 → fwd5 胜率仅16%)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "中段下跌禁区" in src and "pos20" in src


def test_aiinput_has_rs_and_pos_fields():
    """AIInput 必须携带 RS/位置 两个实证因子(供 AI 与门控使用)。"""
    from supermarket.market import AIInput
    fields = AIInput.__dataclass_fields__
    assert "rs20" in fields and "pos20" in fields


def test_daily_direction_uses_closed_candles():
    """2026-09-24 用户: 以15m/1H为主 → 方向权威=已收盘1H(剔除进行中1H棒防边界抖动);
    4H 降为中趋势参考, 日线=背景(仅深跌禁区参考)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "market.py").read_text()
    assert "ind1h_done = df1h.iloc[:-1]" in src, "应剔除进行中的 1H 棒"
    assert "dir_regime" in src and "dir_adx" in src, "方向权威字段缺失"
    assert "dir_regime=ind1h.regime" in src, "1H(已收盘) 应为方向权威字段来源"
    assert 'ind_1h_line=""' in src, "2026-09-25 用户: 指标行应停止渲染(只看价格)"
    assert "ohlc_4h=" in src and "ohlc_1d=" in src, "OHLC 数组保留(4H参考/日线背景)"


def test_trend_progress_fields():
    """趋势推进度字段(AI 与门控使用; 实证: 近5日新高 是回调质量的分水岭)。"""
    from supermarket.market import AIInput
    f = AIInput.__dataclass_fields__
    assert "new_high_5d" in f and "days_since_high" in f
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "market.py").read_text()
    # 2026-09-25 用户: 日线位不渲染; new_high_5d 仍作为程序内部字段保留
    assert "new_high_5d" in src and "日线位(20日)" not in src, "日线位应停止渲染"
    p = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "prompts.py").read_text()
    assert "趋势仍在推进" in p


def test_reconcile_guards():
    """对账双保险(防假补录污染账目): 查询失败跳过 + 可疑记录不补录;
    2026-09-21: 查询成功但空 = 真实空仓必须放行补录(旧安全网拦截导致 OKLO/DDOG 永不入账)。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "对账跳过" in src, "持仓查询失败时应跳过对账"
    assert "对账可疑" in src, "price=0 且 pnl=0 的记录不应补录"
    assert "真实已平, 正常补录" in src, "查询成功空仓应放行补录(空仓≠查询异常)"
    assert "二次确认查询失败" in src, "二次确认失败(网络抖动)应跳过"


def test_no_v2_pending_plans():
    """v2 计划单接口在统一账户下报 40085, 不应再被调用(死代码已删)。"""
    bc = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "bitget_client.py").read_text()
    assert "orders-plan-pending" not in bc
    assert "def v3_strategy_orders" in bc


def test_resilience_fixes():
    """健壮性修复(2026-09-18 体检): 网络重试 + holdings 自愈 + 盈亏比残留修正。"""
    root = Path(__file__).resolve().parent.parent / "src" / "supermarket"
    bc = (root / "bitget_client.py").read_text()
    assert "NET_RETRY" in bc and "NET_RETRY_BACKOFF" in bc, "网络错误应重试"
    eng = (root / "engine.py").read_text()
    assert "holdings 自愈" in eng, "应自动清理陈旧 holdings"
    pr = (root / "prompts.py").read_text()
    assert "盈亏比≥2.0, 中期目标" not in pr, "中期盈亏比残留应清除(短期=1.5)"


def test_optimizations_v2():
    """体检优化(2026-09-18): 数据不足标的黑名单 + 订单流水裁剪。"""
    root = Path(__file__).resolve().parent.parent / "src" / "supermarket"
    eng = (root / "engine.py").read_text()
    assert "_load_data_bad" in eng and "_mark_data_bad" in eng
    assert "s not in _bad" in eng, "候选应过滤数据不足标的"
    ex = (root / "execution.py").read_text()
    assert "orders[-200:]" in ex, "订单流水应裁剪"


def test_no_judgmental_labels_in_input():
    """外部评审采纳(2026-09-19): 输入端不得对量价/盘口打价值判断标签
    (否则与提示词"禁止用洗盘/买压强当理由"自相矛盾, 且带偏注意力)。"""
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "supermarket"
    ind = (root / "indicators.py").read_text()
    # 只检查函数返回的字符串字面量(修复说明的注释不算)
    for lit in re.findall(r'return "([^"]*)"', ind):
        for bad in ("洗盘", "乏力", "风险", "强势"):
            assert bad not in lit, f"量价标签仍含价值判断: {lit}"
    mk = (root / "market.py").read_text()
    assert '"买压"' not in mk and '"卖压"' not in mk, "盘口仍含买卖压标签"


def test_review_adopted_prompt_fixes():
    """采纳的三项提示词改进: 门控短路优先级 / 做空四段式 / 深跌企稳量化门槛。"""
    from supermarket.prompts import SYSTEM_OPEN
    assert "执行优先级" in SYSTEM_OPEN and "价格行为" in SYSTEM_OPEN
    assert "距20日低+x%" in SYSTEM_OPEN, "缺做空四段式模板"
    assert "近 3 日未创新低" in SYSTEM_OPEN, "深跌反转缺量化企稳门槛"


def test_market_refresh_failure_is_conservative():
    """2026-09-24 用户: 大盘日线门控已移除(短期不需要) — 不再日线级禁多;
    实时天气戒备(24h≤-3%/盘中急跌≤-2.5%)仍由扫描前天气门独立把关(fail-safe 仍在天气门)。"""
    mk = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "market.py").read_text()
    assert "去掉大盘日线" in mk, "大盘日线应从输入移除"
    eng = (Path(__file__).resolve().parent.parent / "src" / "supermarket" / "engine.py").read_text()
    assert "不再做日线级禁多门控" in eng, "engine 不应再有大盘日线禁多"
    assert "天气门" in eng and "盘中急跌" in eng, "实时天气戒备必须保留"
def test_pos5d_chase_high_input():
    """2026-09-22: 5日区间位置注入 AI(追高识别); prompts 有贴顶负面清单。"""
    root = Path(__file__).resolve().parent.parent
    mk = (root / "src" / "supermarket" / "market.py").read_text()
    assert "pos5d_pct" in mk and "def _pos5d" in mk
    pr = (root / "src" / "supermarket" / "prompts.py").read_text()
    assert "5日区间位置" in pr and "贴顶" in pr and "追高" in pr


def test_weather_gate_realtime_layers():
    """2026-09-22(用户"大盘要看实时"): 天气门=24h雨(-3%) + 盘中急跌戒备(-2.5%) + 查询失败保守禁开。"""
    root = Path(__file__).resolve().parent.parent
    eng = (root / "src" / "supermarket" / "engine.py").read_text()
    assert "盘中急跌" in eng and "保守禁开新仓" in eng, "天气门缺实时/保守层"
    cfg = (root / "config.yaml").read_text()
    assert "spy_intraday_drop_gate_pct: -2.5" in cfg, "盘中急跌阈值缺失"
    mk = (root / "src" / "supermarket" / "market.py").read_text()
    assert "盘中vs日线MA30" in mk, "market_env 缺实时盘中位置"


def test_short_line_confluence_gate():
    """2026-09-23 BEUSDT 教训(用户"顺势而为"): 1H 短线逆势禁开多(防"周线背景"式接刀);
    顺势需要 4H 方向 + 短线共振。"""
    root = Path(__file__).resolve().parent.parent
    eng = (root / "src" / "supermarket" / "engine.py").read_text()
    assert "1H短线逆势" in eng and "h1_regime" in eng, "缺 1H 逆势门控"
    mk = (root / "src" / "supermarket" / "market.py").read_text()
    assert "h1_regime" in mk, "AIInput 缺 h1_regime"
    pr = (root / "src" / "supermarket" / "prompts.py").read_text()
    assert "只做顺势" in pr and "不要再做回踩" in pr, "顺势唯一, 回踩废弃"
    assert "程序会拦截" in pr, "1H/15m 短线逆势程序拦截"
    assert "周线/日线背景/中长期上涨周期" in pr, "禁长期背景主理由"


def test_hot_pool_cycle_buckets():
    """2026-09-23 用户: 固定热门池 + 周期分桶(上涨/下跌/震荡)把握大方向。"""
    root = Path(__file__).resolve().parent.parent
    eng = (root / "src" / "supermarket" / "engine.py").read_text()
    assert "_hot_pool" in eng and "_cycle_of" in eng and "hot_pool_size" in eng, "缺固定热门池/周期桶"
    assert "热门池分布" in eng, "缺池分布统计"
    pr = (root / "src" / "supermarket" / "prompts.py").read_text()
    assert "周期桶" in pr and "顺着桶的势做" in pr, "缺分桶指导"
    cfg = (root / "config.yaml").read_text()
    assert "hot_pool_size: 25" in cfg


def test_short_operation_cycle_1h_15m():
    """2026-09-23 用户: 短期要多看 1H/15m —— 操作周期=1H/15m, 动量信号改用1H(已收盘), 不再用日线。"""
    root = Path(__file__).resolve().parent.parent
    mk = (root / "src" / "supermarket" / "market.py").read_text()
    assert "_mdf = df1h.iloc[:-1]" in mk, "动量应基于已收盘1H"
    pr = (root / "src" / "supermarket" / "prompts.py").read_text()
    assert "操作周期" in pr and "1H/15m" in pr, "操作主周期=1H/15m 应写入提示词"
    assert "动量/结构在这些级别上判断" in pr
