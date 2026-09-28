"""AI 自迭代(自学习)系统 —— 决策留痕 → 复盘统计 → AI 提案 → 程序校验 → 应用 → 观察/回滚。

设计原则(安全护栏, 2026-09-29 用户要求"程序可以通过 AI 自我调整和迭代"):
  ① 只能改两类东西:
     - 提示词「自学习经验区」(state/live/learned_rules.md, ≤20 条, 每条 ≤200 字)
     - 白名单参数(state/live/tuned_params.json, 每项带范围钳制)
  ② 禁止改: 代码、风控硬约束(TP/SL 上限、名义上限、熔断、门控结构)、执行与安全逻辑。
  ③ 每次 ≤3 条变更; 每日 ≤1 次; 每条必须附数据依据(evidence)。
  ④ 全程版本化(state/live/tuning_log.json): 应用前存快照 → 观察 ≥10 笔 → 更差自动回滚,
     并把该变更标记为"已证伪"(下次提案时告知 AI, 不许重复提)。
  ⑤ 每次变更/回滚 Telegram 通知。

用法:
  - 引擎启动时调用 apply_tuned_overrides(cfg) 应用参数覆盖;
  - 独立定时器(每日)执行: python -m supermarket.self_tune
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from supermarket import notify

LOG = __import__("logging").getLogger("supermarket.self_tune")

STATE_DIR = Path(os.environ.get("SUPERMARKET_STATE_DIR", "state/live"))
RULES_FILE = STATE_DIR / "learned_rules.md"
PARAMS_FILE = STATE_DIR / "tuned_params.json"
LOG_FILE = STATE_DIR / "tuning_log.json"

MAX_RULES = 20
MAX_RULE_CHARS = 200
MAX_CHANGES_PER_RUN = 3
MIN_SAMPLES_BEFORE_EVAL = 10     # 变更后至少积累这么多笔平仓才评估效果
MAX_RULES_DISPLAY = 40           # 给 AI 看的历史证伪条目上限

# 白名单参数 (name → (min, max, 说明))  —— 只允许策略弹性参数, 风控底线永不放开
ALLOWED_PARAMS: dict[str, tuple[float, float, str]] = {
    "momentum_exit_floor": (1.0, 3.0, "动量乏力兑现阈值%(浮盈达到即兑现)"),
    "time_stop_hours": (24, 72, "时间止损小时数"),
    # min_rr 不入白名单: 用户偏好锁定(TP 目标 2~3% + SL 1.5~2% → RR≈1.0),
    # 提高 RR 会直接卡死开仓(2026-09-29 实测: AI 提议 1.5 与用户"两个点"冲突)
    "max_short_positions": (1, 3, "空头最大仓数"),
    "hot_pool_size": (15, 35, "固定热门池大小"),
    "scan_open_every": (1, 6, "开仓扫描间隔轮数"),
}

SYSTEM_TUNER = """你是交易系统的「策略复盘与自调 AI」。你的唯一职责: 基于**真实的成交记录与统计**,
判断当前系统的哪一类决策在亏钱、哪一类在赚钱, 并给出**最小、可验证、可回滚**的调整建议。

可用手段(只有这两类, 其它一律不许提):
1. **经验条目**(写入提示词的自学习区, 系统下一次决策时就会看到): 例如
   "顶部反转信号在常规盘的胜率明显低于亚洲盘, 前者仅在放量长阴吞没时做空" —— 必须来自统计事实;
2. **白名单参数**(仅限下列, 且必须在给定范围内):
{allowed}

硬性要求:
- 每条变更必须给出 evidence: 引用上面统计里**具体分组的具体数字**(笔数/胜率/平均盈亏), 不许空谈;
- 优先"减少亏损"而不是"增加机会"; 样本 < 8 笔的分组不许据此调整(噪声);
- 如果数据显示系统整体正常(只是行情不好), 就返回空 changes —— **不乱调也是正确输出**;
- 单次最多 3 条; 已经"已证伪"的方向不许再提(除非有新数据);
- **不可动的用户偏好**(提了也会被程序拒): 止盈目标 2~3%(TP 上限 10%)、止损 1.5~5%、
  盈亏比下限(不要提议提高 min_rr 或放宽止损)、入场方式(反转后顺势, 不许引入"回踩低吸"或"追趋势中段")、
  空单对称(不许限制做空);"回踩"这类词在被否决的清单上, 不要写进经验条目;
- **只输出一行 JSON**(不要换行、不要 markdown 围栏、不要解释文字);
- 所有字符串内部**不要出现英文双引号**(需要引用时用中文书名号或单引号), 避免 JSON 转义问题;
- 数字必须是纯数字(不要 "2.0%" 这种带符号的字符串)。
格式:
{{"analysis": "一句话: 当前最大的问题是什么(引用数字)",
  "changes": [
    {{"kind": "rule", "op": "add", "text": "经验条目(≤200字)", "rationale": "...", "evidence": "引用统计数字"}},
    {{"kind": "rule", "op": "remove", "text": "要删除的已有条目原文", "rationale": "...", "evidence": "..."}},
    {{"kind": "param", "name": "momentum_exit_floor", "value": 2.0, "rationale": "...", "evidence": "..."}}
  ]}}
若无需调整: {{"analysis": "...", "changes": []}}
"""


# ---------------- 状态读写 ----------------
def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def load_rules() -> list[str]:
    """当前自学习经验条目(每行一条, '#' 开头为注释)。"""
    try:
        if not RULES_FILE.exists():
            return []
        return [ln.strip() for ln in RULES_FILE.read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    except Exception:
        return []


def save_rules(rules: list[str]) -> None:
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(rules[:MAX_RULES])
    RULES_FILE.write_text(
        "# AI 自学习经验区(由 self_tune 自动维护, 可回滚; 每次复盘最多 3 条变更)\n"
        "# 生效: prompts.py 会把它追加到开仓/管仓系统提示词末尾\n" + body + "\n")


def load_params() -> dict[str, float]:
    raw = _load_json(PARAMS_FILE, {})
    out: dict[str, float] = {}
    for k, v in (raw or {}).items():
        if k in ALLOWED_PARAMS:
            lo, hi, _ = ALLOWED_PARAMS[k]
            try:
                out[k] = max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                pass
    return out


def save_params(params: dict[str, float]) -> None:
    _save_json(PARAMS_FILE, params)


def apply_tuned_overrides(cfg: Any) -> dict[str, float]:
    """引擎启动时调用: 把自调参数覆盖到 Config 上(仅白名单, 范围内)。"""
    applied = {}
    for k, v in load_params().items():
        if hasattr(cfg, k):
            old = getattr(cfg, k)
            setattr(cfg, k, type(old)(v) if not isinstance(old, bool) else v)
            applied[k] = v
    if applied:
        LOG.info("自调参数已应用: %s", applied)
    return applied


# ---------------- 证据收集 ----------------
def collect_evidence(memory: Any, recent_n: int = 15) -> dict[str, Any]:
    """从决策账本汇总: 总览 + 按维度分组(反转类型/周期桶/方向/时段/持有时间) + 最近明细。"""
    ds = [d for d in getattr(memory, "decisions", []) if d.get("outcome") == "closed" and d.get("pnl") is not None]
    if not ds:
        return {"total": 0, "groups": {}, "recent": [], "falsified": _load_json(LOG_FILE, {}).get("falsified", [])}

    def _stat(items: list[dict]) -> dict[str, Any]:
        n = len(items)
        wins = sum(1 for x in items if float(x["pnl"]) > 0)
        pnl = sum(float(x["pnl"]) for x in items)
        return {"n": n, "winrate": round(wins / n * 100, 1), "pnl": round(pnl, 4),
                "avg": round(pnl / n, 4)}

    groups: dict[str, dict[str, Any]] = {}
    def _dim(key: str):
        """缺失字段 = 该记录产生于留痕功能上线前(旧记录), 与真正的"未知"区分开。"""
        def fn(d: dict) -> str:
            v = (d.get("params") or {}).get(key)
            return v if v else "旧记录(未留痕)"
        return fn

    dims = {
        "反转类型": _dim("reversal"),
        "周期桶": _dim("cycle"),
        "方向": lambda d: "多" if (d.get("action") == "BUY") else "空",
        "时段": lambda d: d.get("session") or "未知",
        "1H结构": _dim("struct_1h"),
        "动量": _dim("momentum"),
        "持有时间": lambda d: _hold_bucket(d),
    }
    for name, fn in dims.items():
        buckets: dict[str, list[dict]] = {}
        for d in ds:
            try:
                buckets.setdefault(str(fn(d)), []).append(d)
            except Exception:
                pass
        groups[name] = {k: _stat(v) for k, v in buckets.items()}

    recent = []
    for d in ds[-recent_n:]:
        recent.append({
            "ts": time.strftime("%m-%d %H:%M", time.localtime(d.get("ts", 0))),
            "symbol": d.get("symbol"), "action": d.get("action"),
            "entry": d.get("entry"), "close_price": d.get("close_price"),
            "pnl": d.get("pnl"), "close_reason": d.get("close_reason"),
            "reason": (d.get("reason") or "")[:160],
            "features": d.get("params") or {},
        })
    log = _load_json(LOG_FILE, {})
    return {"total": len(ds), "overview": _stat(ds), "groups": groups, "recent": recent,
            "falsified": (log.get("falsified") or [])[-MAX_RULES_DISPLAY:]}


def _hold_bucket(d: dict) -> str:
    t0, t1 = d.get("ts"), d.get("close_ts")
    if not t0 or not t1:
        return "未知"
    h = (float(t1) - float(t0)) / 3600
    if h < 1:
        return "<1h"
    if h < 6:
        return "1-6h"
    if h < 24:
        return "6-24h"
    if h < 48:
        return "1-2天"
    return ">2天"


def render_tuner_prompt(ev: dict[str, Any], rules: list[str], params: dict[str, float]) -> str:
    import json as _json
    lines = ["# 复盘材料", "", "## 1. 当前自学习经验条目(提示词里的)"]
    lines += [f"- {r}" for r in rules] or ["(空)"]
    lines += ["", "## 2. 当前自调参数(白名单内已生效的)"]
    lines += [f"- {k} = {v}" for k, v in params.items()] or ["(空, 使用默认值)"]
    lines += ["", "## 3. 已证伪过的调整方向(不许重复提)"]
    lines += [f"- {x}" for x in (ev.get("falsified") or [])] or ["(无)"]
    lines += ["", "## 4. 成交统计(全部已平仓)"]
    ov = ev.get("overview") or {}
    lines += [f"总览: {ev.get('total', 0)} 笔 | 胜率 {ov.get('winrate')}% | 累计 {ov.get('pnl')} | 平均 {ov.get('avg')}"]
    for dim, buckets in (ev.get("groups") or {}).items():
        lines.append(f"\n[{dim}]")
        for k, st in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"  {k}: {st['n']}笔 胜率{st['winrate']}% 平均{st['avg']} 累计{st['pnl']}")
    lines += ["", "## 5. 最近成交明细(含决策时的特征)"]
    for r in ev.get("recent") or []:
        f = r.get("features") or {}
        feat = f"反转={f.get('reversal','-')} 桶={f.get('cycle','-')} 结构={f.get('struct_1h','-')} 杠杆={f.get('leverage','-')}"
        lines.append(f"  {r['ts']} {r['symbol']} {r['action']} pnl={r['pnl']} 因={r.get('close_reason','')} | {feat}")
        lines.append(f"     理由: {r.get('reason','')}")
    lines += ["", "现在给出一条/多条调整建议(JSON, 见系统提示词约束)。"]
    return "\n".join(lines)


# ---------------- 提案解析与校验 ----------------
def _extract_json(text: str) -> str:
    """从自由文本里提取第一个**括号平衡**的 JSON 对象(字符串感知, 跳过引号内括号)。"""
    s = text
    if "```" in s:
        for p in s.split("```"):
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                s = p
                break
    start = s.find("{")
    if start < 0:
        return s
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def parse_proposal(text: str) -> dict[str, Any]:
    """从 AI 回复里抠出 JSON(容错: 围栏 / 前后噪声 / 括号平衡提取 / 尾随逗号修复)。"""
    if not text:
        return {"analysis": "", "changes": [], "parse_error": "空回复"}
    cand = _extract_json(text)
    attempts = [cand, re.sub(r",\s*([}\]])", r"\1", cand)]
    last_err = ""
    for t in attempts:
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                obj.setdefault("changes", [])
                return obj
        except Exception as e:
            last_err = str(e)[:120]
    return {"analysis": "", "changes": [], "parse_error": last_err}


def validate_proposal(prop: dict[str, Any], rules: list[str],
                      params: dict[str, float] | None = None,
                      ) -> tuple[bool, list[dict], list[str]]:
    """程序校验(护栏): 数量/类型/白名单/范围/长度/重复。返回 (ok, 清洗后的变更, 错误列表)。"""
    errs: list[str] = []
    clean: list[dict] = []
    params = params or load_params()
    ch = prop.get("changes") or []
    if not isinstance(ch, list):
        return False, [], ["changes 必须是数组"]
    if len(ch) > MAX_CHANGES_PER_RUN:
        errs.append(f"变更条数 {len(ch)} > 上限 {MAX_CHANGES_PER_RUN}")
        ch = ch[:MAX_CHANGES_PER_RUN]
    for c in ch:
        if not isinstance(c, dict):
            errs.append("变更项不是对象"); continue
        kind = str(c.get("kind", "")).strip()
        if kind == "rule":
            op = str(c.get("op", "add")).strip()
            text = str(c.get("text", "")).strip()
            if not text:
                errs.append("rule 缺少 text"); continue
            if len(text) > MAX_RULE_CHARS:
                errs.append(f"rule 超长({len(text)}>{MAX_RULE_CHARS})"); continue
            if op == "add":
                if any(text == r for r in rules):
                    errs.append("rule 重复, 跳过"); continue
                if len(rules) + len([x for x in clean if x["op"] == "add"]) >= MAX_RULES:
                    errs.append(f"经验条目已达上限 {MAX_RULES}"); continue
            elif op == "remove":
                if text not in rules:
                    errs.append("remove 目标不存在"); continue
            else:
                errs.append(f"未知 op: {op}"); continue
            clean.append({"kind": "rule", "op": op, "text": text,
                          "rationale": str(c.get("rationale", ""))[:200],
                          "evidence": str(c.get("evidence", ""))[:300]})
        elif kind == "param":
            name = str(c.get("name", "")).strip()
            if name not in ALLOWED_PARAMS:
                errs.append(f"参数 {name} 不在白名单"); continue
            lo, hi, _ = ALLOWED_PARAMS[name]
            try:
                val = float(c.get("value"))
            except (TypeError, ValueError):
                errs.append(f"参数 {name} 值非法"); continue
            if not (lo <= val <= hi):
                errs.append(f"参数 {name}={val} 超出范围 [{lo},{hi}]"); continue
            if name in params and abs(float(params[name]) - val) < 1e-9:
                LOG.info("参数 %s 已是 %s, 跳过(无变化)", name, val)
                continue
            clean.append({"kind": "param", "name": name, "value": val,
                          "rationale": str(c.get("rationale", ""))[:200],
                          "evidence": str(c.get("evidence", ""))[:300]})
        else:
            errs.append(f"未知变更类型: {kind}")
    return (len(clean) > 0, clean, errs)


# ---------------- 应用 / 评估 / 回滚 ----------------
def apply_changes(changes: list[dict], ev: dict[str, Any]) -> dict[str, Any]:
    """应用变更: 先存快照(供回滚) → 写规则/参数 → 记录日志 → 通知。"""
    rules = load_rules()
    params = load_params()
    log = _load_json(LOG_FILE, {"versions": [], "falsified": []})
    entry = {
        "version": len(log.get("versions", [])) + 1,
        "ts": time.time(),
        "applied": [],
        "baseline": {"total": ev.get("total", 0), "overview": ev.get("overview", {})},
        "snapshot": {"rules": list(rules), "params": dict(params)},
        "eval": None,
    }
    for c in changes:
        if c["kind"] == "rule":
            if c["op"] == "add":
                rules.append(c["text"])
            else:
                rules = [r for r in rules if r != c["text"]]
            entry["applied"].append({"kind": "rule", "op": c["op"], "text": c["text"],
                                     "evidence": c.get("evidence", "")})
        else:
            params[c["name"]] = c["value"]
            entry["applied"].append({"kind": "param", "name": c["name"], "value": c["value"],
                                     "evidence": c.get("evidence", "")})
    save_rules(rules[:MAX_RULES])
    save_params(params)
    log.setdefault("versions", []).append(entry)
    _save_json(LOG_FILE, log)
    summary = "; ".join(
        (f"规则{'增' if x['op'] == 'add' else '删'}: {x['text'][:60]}" if x["kind"] == "rule"
         else f"参数 {x['name']}={x['value']}") for x in entry["applied"])
    LOG.warning("自迭代 v%s 已应用: %s", entry["version"], summary)
    try:
        notify.risk_event(f"🤖 AI 自迭代 v{entry['version']} 已应用(可回滚): {summary}")
    except Exception:
        pass
    return entry


def evaluate_and_maybe_rollback(memory: Any) -> dict[str, Any] | None:
    """评估上一次变更: 变更后累计 ≥MIN_SAMPLES_BEFORE_EVAL 笔 → 与基线比, 更差则回滚。"""
    log = _load_json(LOG_FILE, {"versions": [], "falsified": []})
    versions = log.get("versions", [])
    if not versions:
        return None
    last = None
    for v in reversed(versions):
        if v.get("eval") is None:
            last = v
            break
    if last is None:
        return None
    base_total = int((last.get("baseline") or {}).get("total", 0))
    ds = [d for d in getattr(memory, "decisions", [])
          if d.get("outcome") == "closed" and d.get("pnl") is not None]
    after = ds[base_total:]
    if len(after) < MIN_SAMPLES_BEFORE_EVAL:
        return None
    n = len(after)
    pnl = sum(float(x["pnl"]) for x in after)
    wins = sum(1 for x in after if float(x["pnl"]) > 0)
    avg = pnl / n
    base_ov = (last.get("baseline") or {}).get("overview") or {}
    base_avg = float(base_ov.get("avg") or 0.0)
    last["eval"] = {"n": n, "avg": round(avg, 4), "winrate": round(wins / n * 100, 1),
                    "base_avg": base_avg, "verdict": "keep" if avg >= base_avg else "rollback"}
    if avg < base_avg:
        snap = last.get("snapshot") or {}
        save_rules(list(snap.get("rules") or []))
        save_params(dict(snap.get("params") or {}))
        last["rolled_back"] = True
        log.setdefault("falsified", []).append(
            f"v{last['version']} 已回滚(变更后 {n} 笔平均 {avg:+.4f} < 基线 {base_avg:+.4f}): "
            + "; ".join(f"{x.get('text') or x.get('name')}" for x in last.get("applied", []))[:160])
        _save_json(LOG_FILE, log)
        LOG.warning("自迭代 v%s 效果变差(平均 %+.4f < 基线 %+.4f) → 已回滚", last["version"], avg, base_avg)
        try:
            notify.risk_event(f"↩️ AI 自迭代 v{last['version']} 效果变差已自动回滚"
                              f"(变更后 {n} 笔平均 {avg:+.4f} vs 基线 {base_avg:+.4f})")
        except Exception:
            pass
        return last
    _save_json(LOG_FILE, log)
    LOG.info("自迭代 v%s 效果保持(平均 %+.4f ≥ 基线 %+.4f)", last["version"], avg, base_avg)
    return last


# ---------------- 主入口 ----------------
def _allowed_text() -> str:
    return "\n".join(f"   - {k}: [{lo}, {hi}]  {desc}" for k, (lo, hi, desc) in ALLOWED_PARAMS.items())


def run_self_tune(cfg: Any, memory: Any, dry_run: bool = False) -> dict[str, Any]:
    """复盘一次 → AI 提案 → 校验 → 应用(或 dry_run 只打印)。"""
    import httpx
    result: dict[str, Any] = {"applied": None, "proposal": None, "errors": []}
    # 1) 先评估上一版(可能回滚)
    ev_roll = evaluate_and_maybe_rollback(memory)
    if ev_roll is not None and ev_roll.get("rolled_back"):
        result["rollback"] = ev_roll["version"]
    # 2) 收集证据
    ev = collect_evidence(memory)
    if ev.get("total", 0) < 8:
        LOG.info("自迭代: 样本不足(%s 笔 < 8), 跳过", ev.get("total", 0))
        result["skipped"] = "样本不足"
        return result
    # 3) AI 提案
    rules, params = load_rules(), load_params()
    prompt = render_tuner_prompt(ev, rules, params)
    hdr = {
        "Authorization": f"Bearer {cfg.llm.api_key}",
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "x-opencode-session": os.environ.get("LLM_SESSION", "selftune"),
    }
    sys_msg = SYSTEM_TUNER.format(allowed=_allowed_text())
    prop: dict[str, Any] = {}
    for attempt in range(2):
        msgs = [{"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt}]
        if attempt == 1:
            msgs.append({"role": "user", "content":
                         "上一次回复不是合法 JSON(解析错误)。请严格只输出一行合法 JSON, "
                         "字符串内不要出现英文双引号与换行。"})
        body = {"model": cfg.llm.model, "messages": msgs, "temperature": 0.2,
                "max_tokens": int(getattr(cfg.llm, "max_tokens", 16000))}
        try:
            r = httpx.post(cfg.llm.base_url.rstrip("/") + "/chat/completions", headers=hdr,
                           json=body, timeout=180)
            r.raise_for_status()
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception as e:
            LOG.error("自迭代: AI 调用失败 %s", str(e)[:120])
            result["errors"].append(f"AI 调用失败: {str(e)[:120]}")
            return result
        prop = parse_proposal(txt)
        if not prop.get("parse_error"):
            break
        LOG.warning("自迭代: 第 %d 次 JSON 解析失败(%s), 重试", attempt + 1, prop["parse_error"])
    result["proposal"] = prop
    if prop.get("parse_error"):
        result["errors"].append(f"解析失败(已重试): {prop['parse_error']}")
        return result
    ok, clean, errs = validate_proposal(prop, rules, params)
    result["errors"] = errs
    if not ok:
        LOG.info("自迭代: 无有效变更(analysis=%s, errs=%s)", str(prop.get("analysis"))[:80], errs[:2])
        return result
    if dry_run:
        LOG.info("[dry-run] 拟应用 %d 条: %s", len(clean), clean)
        return result
    result["applied"] = apply_changes(clean, ev)
    return result


def main() -> None:
    """独立入口: python -m supermarket.self_tune [--dry-run]"""
    import argparse
    from supermarket.config import Config
    from supermarket.memory import AIMemory
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    cfg = Config.load()
    apply_tuned_overrides(cfg)
    # 与引擎一致: 实盘状态在 state/live(引擎构造时取 base/live)
    base = Path(cfg.state_path)
    live = base / "live"
    mem = AIMemory(live if live.exists() else base)
    res = run_self_tune(cfg, mem, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str)[:2000])


if __name__ == "__main__":
    main()
