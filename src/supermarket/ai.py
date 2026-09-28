"""AI 决策层: Provider 接口 + OpenAI 兼容实现 + 容错解析 + 自动 HOLD 兜底。

铁律(来自 ai-native-trading skill):
- AI 是唯一决策者; 任何 provider 失败/解析失败 → 该标的本次决策 = HOLD(不开仓=最安全)
- json_mode=False(flash 在 json_mode 下会截断), max_tokens 足够, 提示"不要思考直接输出JSON"
- 5xx/网络重试, 4xx 不重试; 类级熔断: 连续失败 N 次 → 暂停 M 秒
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_MAX_RETRIES_DEFAULT = 3


# ---------- 决策数据结构 ----------
@dataclass
class OpenDecision:
    action: str = "HOLD"          # BUY / SELL / HOLD
    leverage: int | None = None   # AI 决定的风险密度(3~20x; 2026-09-19 用户要求)
    margin_usd: float | None = None  # AI 决定的仓位大小(每批保证金 0.5~5$; 2026-09-22 用户要求)
    max_positions: int | None = None  # AI 决定的最大总仓数(1~6; 2026-09-22 用户澄清: 最多开几个仓)
    max_notional_mult: float | None = None  # AI 决定的总名义倍数(3~6x净值=资金利用率; 2026-09-22 用户)
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    raw: str = ""                 # 原始模型输出(审计)

    @property
    def is_buy(self) -> bool:
        return self.action == "BUY"

    @property
    def is_short(self) -> bool:
        return self.action == "SELL"


@dataclass
class ManageDecision:
    action: str = "HOLD"          # HOLD / ADJUST / CLOSE
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    raw: str = ""

    @property
    def is_close(self) -> bool:
        return self.action == "CLOSE"


# ---------- JSON 容错解析 ----------
def _extract_json_block(text: str) -> str:
    """提取首个 { 到最后一个 } 之间的内容(容忍 ```json 代码块/前后说明/截断无闭合符)。"""
    s = text.find("{")
    if s == -1:
        return ""
    e = text.rfind("}")
    if e == -1:
        return text[s:]  # 截断: 无闭合括号, 交给 _fix_truncated_json
    return text[s:e + 1]


def _fix_truncated_json(block: str) -> str:
    """截断的 JSON: 渐进截断尾字符, 尝试补全闭合括号, 保留最长合法前缀。"""
    b = block.rstrip()
    for cut in range(len(b), 0, -1):
        cand = b[:cut].rstrip().rstrip(",")
        for closer in ("", "}"):
            trial = cand + closer
            try:
                json.loads(trial)
                return trial
            except json.JSONDecodeError:
                continue
    return ""


def parse_open_decision(raw: str) -> OpenDecision:
    """解析 open 决策 JSON: {"action","stop_loss","take_profit","reason"}。"""
    text = (raw or "").strip()
    block = _extract_json_block(text)
    obj: dict[str, Any] = {}
    if block:
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            try:
                obj = json.loads(_fix_truncated_json(block))
            except json.JSONDecodeError:
                # 正则兜底(作用于原始文本)
                act = re.search(r'"action"\s*:\s*"([A-Za-z]+)"', text)
                sl = re.search(r'"stop_loss"\s*:\s*([\d.]+)', text)
                tp = re.search(r'"take_profit"\s*:\s*([\d.]+)', text)
                rs = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
                lv = re.search(r'"leverage"\s*:\s*(\d+)', text)
                mg = re.search(r'"margin_usd"\s*:\s*([\d.]+)', text)
                mp = re.search(r'"max_positions"\s*:\s*(\d+)', text)
                mm = re.search(r'"max_notional_mult"\s*:\s*([\d.]+)', text)
                obj = {
                    "action": act.group(1) if act else "HOLD",
                    "leverage": int(lv.group(1)) if lv else None,
                    "margin_usd": float(mg.group(1)) if mg else None,
                    "max_positions": int(mp.group(1)) if mp else None,
                    "max_notional_mult": float(mm.group(1)) if mm else None,
                    "stop_loss": float(sl.group(1)) if sl else None,
                    "take_profit": float(tp.group(1)) if tp else None,
                    "reason": rs.group(1) if rs else (text[:80] if text else ""),
                }
    action = str(obj.get("action", "HOLD")).upper().strip()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    try:
        _lv = obj.get("leverage")
        leverage = int(float(_lv)) if _lv not in (None, "") else None
    except (TypeError, ValueError):
        leverage = None
    try:
        _mg = obj.get("margin_usd")
        margin_usd = float(_mg) if _mg not in (None, "") else None
    except (TypeError, ValueError):
        margin_usd = None
    try:
        _mp = obj.get("max_positions")
        max_positions = int(float(_mp)) if _mp not in (None, "") else None
    except (TypeError, ValueError):
        max_positions = None
    try:
        _mm = obj.get("max_notional_mult")
        max_notional_mult = float(_mm) if _mm not in (None, "") else None
    except (TypeError, ValueError):
        max_notional_mult = None
    try:
        sl = float(obj.get("stop_loss")) if obj.get("stop_loss") not in (None, "") else None
    except (TypeError, ValueError):
        sl = None
    try:
        tp = float(obj.get("take_profit")) if obj.get("take_profit") not in (None, "") else None
    except (TypeError, ValueError):
        tp = None
    return OpenDecision(action=action, leverage=leverage, margin_usd=margin_usd,
                        max_positions=max_positions, max_notional_mult=max_notional_mult,
                        stop_loss=sl, take_profit=tp,
                        reason=str(obj.get("reason", "")), raw=text)


def parse_manage_decision(raw: str) -> ManageDecision:
    """解析管仓决策 JSON: {"action":"HOLD|ADJUST|CLOSE","stop_loss","take_profit","reason"}。"""
    text = (raw or "").strip()
    block = _extract_json_block(text)
    obj: dict[str, Any] = {}
    try:
        if block:
            obj = json.loads(block)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_fix_truncated_json(block))
        except json.JSONDecodeError:
            act = re.search(r'"action"\s*:\s*"([A-Za-z]+)"', block)
            sl = re.search(r'"stop_loss"\s*:\s*([\d.]+)', block)
            tp = re.search(r'"take_profit"\s*:\s*([\d.]+)', block)
            obj = {"action": act.group(1) if act else "HOLD",
                   "stop_loss": float(sl.group(1)) if sl else None,
                   "take_profit": float(tp.group(1)) if tp else None}
    action = str(obj.get("action", "HOLD")).upper().strip()
    if action not in ("HOLD", "ADJUST", "CLOSE"):
        action = "HOLD"
    try:
        sl_v = float(obj.get("stop_loss")) if obj.get("stop_loss") not in (None, "") else None
    except (TypeError, ValueError):
        sl_v = None
    try:
        tp_v = float(obj.get("take_profit")) if obj.get("take_profit") not in (None, "") else None
    except (TypeError, ValueError):
        tp_v = None
    return ManageDecision(action=action, stop_loss=sl_v, take_profit=tp_v,
                          reason=str(obj.get("reason", "")), raw=text)


# ---------- Provider 抽象 ----------
class LLMProvider:
    name = "abstract"

    def decide_open(self, system: str, prompt: str) -> OpenDecision: ...  # pragma: no cover
    def decide_manage(self, system: str, prompt: str) -> ManageDecision: ...  # pragma: no cover


class FallbackHOLDProvider(LLMProvider):
    """LLM 不可配置时兜底: 恒 HOLD。开发期/只读验证模式安全默认。"""

    name = "fallback-hold"

    def decide_open(self, system: str, prompt: str) -> OpenDecision:
        return OpenDecision(action="HOLD", reason="LLM 未配置, 兜底 HOLD")

    def decide_manage(self, system: str, prompt: str) -> ManageDecision:
        return ManageDecision(action="HOLD", reason="LLM 未配置, 兜底 HOLD")


class OpenCodeProvider(LLMProvider):
    """OpenAI 兼容 chat/completions 端点(opencode zen/go 等)。"""

    name = "opencode"

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 1200,
                 timeout_s: float = 45.0, max_retries: int = 2,
                 circuit_failures: int = 5, circuit_pause_s: int = 300):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.circuit_failures = circuit_failures
        self.circuit_pause_s = circuit_pause_s
        self._fail_streak = 0
        self._paused_until = 0.0
        self._lock = threading.Lock()  # 并发扫描时保护熔断计数

    def _circuit_open(self) -> bool:
        with self._lock:
            if time.time() < self._paused_until:
                log.warning("AI 熔断中, 剩余 %.0fs", self._paused_until - time.time())
                return True
            if self._fail_streak >= self.circuit_failures:
                self._paused_until = time.time() + self.circuit_pause_s
                self._fail_streak = 0
                log.warning("AI 连续失败 %d 次, 熔断 %ds", self.circuit_failures, self.circuit_pause_s)
                return True
            return False

    def _record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._fail_streak = 0
            else:
                self._fail_streak += 1

    def _chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY 未配置")
        if self._circuit_open():
            raise RuntimeError("AI circuit open")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ai-trader/0.1",
                   # Console Go 提供商要求: 缺此头报 MissingSessionID(实测 2026-09)
                   "x-opencode-session": str(uuid.uuid4())}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(1.5 * attempt)
            try:
                with httpx.Client(timeout=httpx.Timeout(self.timeout_s, connect=10.0)) as client:
                    resp = client.post(url, json=payload, headers=headers)
            except Exception as e:  # 网络/超时 → 可重试
                last_err = e
                log.warning('AI 网络失败(第%d次, model=%s, timeout=%.0fs): %s',
                            attempt + 1, self.model, self.timeout_s, str(e)[:120])
                continue
            if 400 <= resp.status_code < 500:
                # 4xx = 密钥/额度/参数问题, 重试无意义且会拖慢整轮扫描 → 立即失败(铁律: 4xx 不重试)
                self._record(False)
                raise RuntimeError('AI 请求被拒(HTTP %s, 4xx 不重试, model=%s): %s'
                                   % (resp.status_code, self.model, resp.text[:120]))
            if resp.status_code >= 500:
                last_err = RuntimeError('HTTP %s: %s' % (resp.status_code, resp.text[:120]))
                log.warning('AI 服务端失败(第%d次, model=%s): %s',
                            attempt + 1, self.model, str(last_err)[:120])
                continue
            try:
                data = resp.json()
            except Exception as e:  # 非 JSON 响应 → 可重试
                last_err = e
                log.warning('AI 响应非 JSON(第%d次, model=%s): %s',
                            attempt + 1, self.model, str(e)[:120])
                continue
            content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
            if not content:
                # 思考型模型额度被 reasoning 吃光时的典型表现, 值得单独留痕
                last_err = RuntimeError('empty content')
                log.warning('AI 空响应(第%d次, model=%s): %s',
                            attempt + 1, self.model, str(last_err)[:120])
                continue
            self._record(True)
            return content
        self._record(False)
        raise RuntimeError('AI 调用最终失败: %s' % last_err)

    def decide_open(self, system: str, prompt: str) -> OpenDecision:
        try:
            return parse_open_decision(self._chat(system, prompt))
        except Exception as e:
            log.error("open 决策失败 → HOLD: %s", str(e)[:120])
            return OpenDecision(action="HOLD", reason=f"AI 调用失败: {str(e)[:60]}")

    def decide_manage(self, system: str, prompt: str) -> ManageDecision:
        try:
            return parse_manage_decision(self._chat(system, prompt))
        except Exception as e:
            log.error("manage 决策失败 → HOLD: %s", str(e)[:120])
            return ManageDecision(action="HOLD", reason=f"AI 调用失败: {str(e)[:60]}")


def build_provider(cfg) -> LLMProvider:
    """按配置构建 provider: LLM 未配置 → FallbackHOLD。"""
    llm = cfg.llm
    if llm.ready:
        # 思考型模型(deepseek-v4.1-flash)单轮实测约 58s; read timeout 低于该量级会让每次
        # 决策必然超时 → 全部静默降级 HOLD(2026-09-28 实盘故障根因)。config.py 默认值仍是
        # 45.0 且不可改, 故在此设下限, 防止 .env/config.yaml 漂移后故障复现。
        timeout_s = max(float(llm.timeout_s or 0.0), 120.0)
        return OpenCodeProvider(llm.base_url, llm.api_key, llm.model,
                                llm.temperature, llm.max_tokens,
                                timeout_s, llm.max_retries,
                                llm.circuit_failures, llm.circuit_pause_s)
    return FallbackHOLDProvider()