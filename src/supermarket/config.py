"""配置加载: yaml + 环境变量注入 (${VAR} 语法)。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_dotenv(path: str | Path) -> None:
    """把 .env 注入 os.environ(不覆盖已有变量)。支持 KEY=VALUE 与引号。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _inject_env(obj: Any) -> Any:
    """递归替换 ${VAR} 占位符。缺失的环境变量 → 空串(调用方校验)。"""
    if isinstance(obj, dict):
        return {k: _inject_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_inject_env(v) for v in obj]
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), "")
        return _ENV_RE.sub(repl, obj)
    return obj


@dataclass
class BitgetCfg:
    base_url: str = "https://api.bitget.com"
    api_key: str = ""
    secret: str = ""
    passphrase: str = ""
    timeout_s: int = 20

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.secret and self.passphrase)


@dataclass
class LLMCfg:
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-v4-flash"
    temperature: float = 0.3
    max_tokens: int = 1200
    timeout_s: float = 45.0
    max_retries: int = 3
    circuit_failures: int = 5
    circuit_pause_s: int = 300

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass
class PaperCfg:
    initial_equity: float = 30.0
    taker_fee: float = 0.0006
    maker_fee: float = 0.0002


@dataclass
class Config:
    mode: str = "paper"
    scan_interval: int = 1800          # 兼容字段(默认/隔夜)
    # 动态节拍(2026-09 用户要求: 盘中缩短读取间隔, 及时控制止盈止损)
    interval_regular: int = 300        # 美股盘中: 5 分钟(管仓)
    interval_prepost: int = 900        # 盘前/盘后: 15 分钟
    interval_closed: int = 1800        # 隔夜: 30 分钟
    scan_open_every: int = 3           # 开仓扫描每 N 轮一次(管仓每轮都跑)
    momentum_exit_floor: float = 1.5   # 程序兜底: 动量"乏力"且浮盈≥此值 → 直接兑现(实证回吐率66.2%)
    skip_weekend: bool = True
    max_symbols_per_round: int = 15
    margin_per_trade_usd: float = 2.0
    margin_min_usd: float = 0.5
    margin_max_usd: float = 5.0
    leverage: int = 20
    leverage_min: int = 3
    margin_mode: str = "crossed"
    max_notional_mult: float = 6.0
    max_positions_divisor: int = 10  # 保留(兼容); 实际仓数由 risk.py 按 净值×6÷每仓 推导
    max_short_positions: int = 3
    max_batches_per_symbol: int = 3   # 分批建仓上限(超市补货: $2×3=6保证金/标的)
    market_down_adx: float = 25.0     # 大盘趋势门控: SPY日线ADX≥此值且趋势向下 → 禁开多仓
    spy_drop_gate_pct: float = -3.0
    spy_intraday_drop_gate_pct: float = -2.5   # 天气门: SPY 24h 跌幅超过此值 → 当日禁开新仓
    min_turnover_floor: float = 300_000.0
    # K线取量(按周期需求定, 非统一200): 5m=入场时机(8h覆盖) 1H=趋势(10天)
    # 4H=中趋势(50天) 1D=方向权威(90=Bitget上限) 1W=季节视角(13=上限)
    kline_limits: dict[str, int] = field(
        default_factory=lambda: {"5m": 100, "1H": 240, "4H": 300, "1D": 90, "1W": 13})
    sl_min_pct: float = 1.5    # 短期v3.0(2026-09-22 用户): 止损下限1.5%
    sl_max_pct: float = 5.0
    min_rr: float = 1.5
    tp_max_pct: float = 10.0        # 短期: RR≥1.5(紧止损快兑现, 靠胜率)
    time_stop_hours: float = 48.0  # 时间止损: 满48h未平 → 强平(防滞销)
    stop_repost_diff_pct: float = 0.2
    max_daily_drawdown_pct: float = 5.0    # 当日亏损≥净值5% → 停开新仓(原30%过宽)
    max_consecutive_losses: int = 3
    pause_after_loss_minutes: int = 120
    symbol_pool: str | list[str] = "AUTO"
    hot_symbols: list[str] = field(default_factory=list)
    hot_pool_size: int = 25      # 固定热门池规模(用户2026-09-23: 二三十个热门)
    llm: LLMCfg = field(default_factory=LLMCfg)
    scan_workers: int = 4
    bitget: BitgetCfg = field(default_factory=BitgetCfg)
    state_dir: str = "state"
    paper: PaperCfg = field(default_factory=PaperCfg)

    def interval_for_session(self, session: str) -> int:
        """按美股时段返回本轮间隔(秒)。"""
        if session == "regular":
            return self.interval_regular
        if session in ("pre_market", "post_market"):
            return self.interval_prepost
        return self.interval_closed

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        p = Path(path)
        raw: dict[str, Any] = {}
        if p.exists():
            load_dotenv(".env")
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = _inject_env(raw)

        def g(key: str, default: Any = None) -> Any:
            return raw.get(key, default)

        llm_raw = g("llm", {}) or {}
        bg_raw = g("bitget", {}) or {}
        paper_raw = g("paper", {}) or {}
        return cls(
            mode=str(g("mode", "paper")).lower(),
            scan_interval=int(g("scan_interval", 1800)),
            interval_regular=int(g("interval_regular", 300)),
            interval_prepost=int(g("interval_prepost", 900)),
            interval_closed=int(g("interval_closed", 1800)),
            scan_open_every=int(g("scan_open_every", 3)),
            momentum_exit_floor=float(g("momentum_exit_floor", 1.5)),
            skip_weekend=bool(g("skip_weekend", True)),
            max_symbols_per_round=int(g("max_symbols_per_round", 15)),
            scan_workers=int(g("scan_workers", 4)),
            margin_per_trade_usd=float(g("margin_per_trade_usd", 2.0)),
            margin_min_usd=float(g("margin_min_usd", 0.5)),
            margin_max_usd=float(g("margin_max_usd", 5.0)),
            leverage=int(g("leverage", 20)),
            leverage_min=int(g("leverage_min", 3)),
            margin_mode=str(g("margin_mode", "crossed")).lower(),
            max_notional_mult=float(g("max_notional_mult", 6.0)),
            max_positions_divisor=int(g("max_positions_divisor", 10)),
            max_short_positions=int(g("max_short_positions", 3)),
            max_batches_per_symbol=int(g("max_batches_per_symbol", 3)),
            spy_drop_gate_pct=float(g("spy_drop_gate_pct", -3.0)),
            spy_intraday_drop_gate_pct=float(g("spy_intraday_drop_gate_pct", -2.5)),
            min_turnover_floor=float(g("min_turnover_floor", 5_000_000.0)),
            kline_limits=dict(g("kline_limits") or {"5m": 100, "1H": 240, "4H": 300, "1D": 90, "1W": 13}),
            sl_min_pct=float(g("sl_min_pct", 1.5)),
            sl_max_pct=float(g("sl_max_pct", 5.0)),
            min_rr=float(g("min_rr", 1.0)),
            tp_max_pct=float(g("tp_max_pct", 10.0)),
            time_stop_hours=float(g("time_stop_hours", 48.0)),
            stop_repost_diff_pct=float(g("stop_repost_diff_pct", 0.2)),
            max_daily_drawdown_pct=float(g("max_daily_drawdown_pct", 5.0)),
            max_consecutive_losses=int(g("max_consecutive_losses", 3)),
            pause_after_loss_minutes=int(g("pause_after_loss_minutes", 120)),
            symbol_pool=g("symbol_pool", "AUTO"),
            hot_symbols=list(g("hot_symbols", []) or []),
            hot_pool_size=int(g("hot_pool_size", 25)),
            llm=LLMCfg(
                base_url=str(llm_raw.get("base_url", "")),
                api_key=str(llm_raw.get("api_key", "")),
                model=str(llm_raw.get("model", "deepseek-v4-flash")),
                temperature=float(llm_raw.get("temperature", 0.3)),
                max_tokens=int(llm_raw.get("max_tokens", 1200)),
                timeout_s=float(llm_raw.get("timeout_s", 45.0)),
                max_retries=int(llm_raw.get("max_retries", 2)),
                circuit_failures=int(llm_raw.get("circuit_failures", 5)),
                circuit_pause_s=int(llm_raw.get("circuit_pause_s", 300)),
            ),
            bitget=BitgetCfg(
                base_url=str(bg_raw.get("base_url", "https://api.bitget.com")),
                api_key=str(bg_raw.get("api_key", "")),
                secret=str(bg_raw.get("secret", "")),
                passphrase=str(bg_raw.get("passphrase", "")),
                timeout_s=int(bg_raw.get("timeout_s", 20)),
            ),
            state_dir=str(g("state_dir", "state")),
            paper=PaperCfg(
                initial_equity=float(paper_raw.get("initial_equity", 30.0)),
                taker_fee=float(paper_raw.get("taker_fee", 0.0006)),
                maker_fee=float(paper_raw.get("maker_fee", 0.0002)),
            ),
        )

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p