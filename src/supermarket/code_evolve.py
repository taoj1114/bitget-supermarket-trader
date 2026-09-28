"""AI 代码自迭代(code evolve)—— 让程序像维护者一样自己发现并修复问题/补功能。

与 self_tune 的分工:
  - self_tune  : 调「策略弹性」(提示词经验区 + 白名单参数) —— 不改代码
  - code_evolve: 改「程序本身」—— 修 bug、消除不合理设计、补必要功能 —— 直接动源码

闭环(每日一次, 定时器 supermarket-evolve.timer):
  ① 健康检查: 上一轮改动后是否出现异常(错误激增/服务掉线) → 异常则自动回滚到上一个 tag
  ② 信号收集: 近期 ERROR/Traceback 日志 + 服务状态 + 测试结果 + 自迭代统计摘要
  ③ AI 提案: 结构化 JSON —— findings + 搜索替换式改动(省 token、可精确应用)
  ④ 硬护栏(程序校验, 不通过即拒):
     - 保护清单: bitget_client.py(签名/接口)、risk.py(风控底线)、execution.py(保护单/裸仓兜底)、
       config.py(底线参数) —— 永不自动改
     - 既有测试文件永不改(防止"改测试让自己通过"); 只允许新增 tests/test_evolve_*.py
     - 路径白名单: src/supermarket/、scripts/、docs/、README.md、tests/test_evolve_*.py
     - 改动条数 ≤ 6; 每条 find 必须在文件中唯一存在
  ⑤ 测试闸门: 应用后必须 `pytest` 全绿 + 语法检查; 任何失败 → 立即 revert, 不留痕迹
  ⑥ 上线: 打 tag(evolve-<时间>) → 提交 → 重启服务 → 通知 Telegram
  ⑦ 下一轮回看: 若改动后健康状况变差 → revert 到上一个 evolve tag 并通知

用法:
  python -m supermarket.code_evolve [--dry-run]     # 手动跑一次
  systemctl status supermarket-evolve.timer         # 定时器
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from supermarket import notify

LOG = __import__("logging").getLogger("supermarket.code_evolve")

ROOT = Path(os.environ.get("SUPERMARKET_ROOT", ".")).resolve()
STATE_DIR = Path(os.environ.get("SUPERMARKET_STATE_DIR", "state/live"))
EVOLVE_LOG = STATE_DIR / "code_evolve_log.json"

MAX_EDITS = 6
HEALTH_ERROR_THRESHOLD = 25       # 改动后日志 ERROR 超过此数 → 视为不健康
HEALTH_WINDOW_HOURS = 24

# 永不自动改(安全逻辑与风控底线)
PROTECTED_FILES = {
    "src/supermarket/bitget_client.py",   # 签名/交易所接口
    "src/supermarket/risk.py",            # 风控底线校验
    "src/supermarket/execution.py",       # TPSL/裸仓兜底/平仓
    "src/supermarket/config.py",          # 配置与底线默认值
    "src/supermarket/code_evolve.py",     # 自迭代自身
    "src/supermarket/self_tune.py",       # 自迭代自身
}
ALLOWED_PREFIXES = ("src/supermarket/", "scripts/", "docs/")
ALLOWED_EXACT = ("README.md",)

SYSTEM_EVOLVE = """你是这个交易系统的**维护者 AI**(唯一职责: 让系统更正确、更稳健)。

输入会给你: 近期错误日志、服务状态、测试结果、交易统计摘要、以及相关文件的结构与片段。
你的任务: 找出**真实的 bug / 不合理的设计**, 必要时补上**必要的小功能**。输出结构化 JSON:

{
  "findings": [
    {"kind": "bug|design|feature", "severity": "high|medium|low",
     "what": "一句话问题", "evidence": "日志/代码/统计里的事实依据", "file": "src/supermarket/xxx.py"}
  ],
  "edits": [
    {"file": "src/supermarket/xxx.py",
     "find": "文件中**唯一存在**的原文片段(要足够长以保唯一)",
     "replace": "替换后的新代码",
     "why": "为什么这样改(对应哪个 finding)"}
  ],
  "test_note": "需要新增的验证点(会写成 tests/test_evolve_*.py)",
  "need_files": ["src/supermarket/engine.py:1180-1240", "src/supermarket/market.py:220-260"]
}

**探查协议**: 如果你需要看具体代码才能给出精确改动, 把文件与行号写进 need_files
(例如 "src/supermarket/engine.py:1180-1240"), 系统会把那些片段原文发给你, 你在下一轮再给 edits。
最多探查 3 轮; 请优先申请**与 findings 直接相关**的小片段(每轮总量有限)。

硬性规则(违反会被程序拒绝):
- **禁止**修改以下文件: bitget_client.py / risk.py / execution.py / config.py / self_tune.py / code_evolve.py;
- **禁止**修改任何既有 tests/test_*.py(只能通过 test_note 描述新增测试);
- **禁止**削弱任何安全逻辑(裸仓兜底/门控/对账/熔断)与风控底线(TP 上限 10%/SL 上限 5%/名义上限/空单上限);
- **禁止**引入新的第三方依赖(只用 httpx / pandas / numpy / pyyaml / 标准库);
- edits 最多 {max_edits} 条; 每条 find 必须是原文件中**唯一**出现的片段(否则程序无法安全替换);
- **没有真问题时, 宁可返回空 edits**(findings 可以照写) —— 不乱改代码同样是好维护者;
- 只输出一行 JSON(无 markdown 围栏、无解释文字、字符串内不要出现英文双引号)。
"""


# ---------------- 工具 ----------------
def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)[:200]


def _load_log() -> dict[str, Any]:
    try:
        return json.loads(EVOLVE_LOG.read_text())
    except Exception:
        return {"rounds": [], "reverted": []}


def _save_log(d: dict[str, Any]) -> None:
    EVOLVE_LOG.parent.mkdir(parents=True, exist_ok=True)
    EVOLVE_LOG.write_text(json.dumps(d, ensure_ascii=False, indent=1))


# ---------------- ① 健康检查 / 回滚 ----------------
def check_health() -> dict[str, Any]:
    """上一轮改动后是否健康: 服务是否 active、ERROR 数量是否正常。"""
    out: dict[str, Any] = {"healthy": True, "reasons": []}
    code, s = _run(["systemctl", "is-active", "supermarket-live"], timeout=20)
    if "active" not in s:
        out["healthy"] = False
        out["reasons"].append(f"服务非 active: {s.strip()[:40]}")
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - HEALTH_WINDOW_HOURS * 3600))
    code, s = _run(["journalctl", "-u", "supermarket-live", "--since", since, "--no-pager"], timeout=40)
    errs = [ln for ln in s.splitlines() if " ERROR " in ln and "AI 调用最终失败" not in ln]
    out["error_count"] = len(errs)
    if len(errs) > HEALTH_ERROR_THRESHOLD:
        out["healthy"] = False
        out["reasons"].append(f"近 {HEALTH_WINDOW_HOURS}h ERROR 数 {len(errs)} > {HEALTH_ERROR_THRESHOLD}")
        out["sample_errors"] = errs[-6:]
    return out


def rollback_last_round(reason: str) -> bool:
    """回滚到上一个 evolve tag(改动前状态)并重启服务。"""
    code, tags = _run(["git", "tag", "--list", "evolve-*", "--sort=-creatordate"], timeout=30)
    tags = [t.strip() for t in tags.splitlines() if t.strip()]
    if not tags:
        return False
    last = tags[0]
    code, out = _run(["git", "checkout", "-f", last], timeout=60)
    if code != 0:
        LOG.error("回滚 checkout 失败: %s", out[:200])
        return False
    _run(["systemctl", "restart", "supermarket-live"], timeout=90)
    d = _load_log()
    d.setdefault("reverted", []).append({"ts": time.time(), "tag": last, "reason": reason[:200]})
    _save_log(d)
    LOG.warning("已回滚到 %s(原因: %s)", last, reason[:120])
    try:
        notify.risk_event(f"↩️ 代码自迭代已回滚到 {last}\n原因: {reason[:150]}")
    except Exception:
        pass
    return True


# ---------------- ② 信号收集 ----------------
def collect_signals() -> dict[str, Any]:
    sig: dict[str, Any] = {}
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 24 * 3600))
    code, s = _run(["journalctl", "-u", "supermarket-live", "--since", since, "--no-pager"], timeout=60)
    lines = s.splitlines()
    errs = [ln for ln in lines if " ERROR " in ln]
    # 去重(同类错误只留样例)
    seen: set[str] = set()
    uniq: list[str] = []
    for ln in errs:
        key = re.sub(r"[\d.]+", "N", ln)[-120:]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ln[-220:])
    sig["errors_unique"] = uniq[:15]
    sig["error_total"] = len(errs)
    code, s2 = _run(["journalctl", "-u", "supermarket-live", "-n", "40", "--no-pager"], timeout=30)
    sig["recent_lines"] = [ln[-200:] for ln in s2.splitlines()[-25:]]
    code, s3 = _run([".venv/bin/python", "-m", "pytest", "tests/", "-q"], timeout=900)
    sig["tests_tail"] = (s3 or "").strip().splitlines()[-3:]
    code, s4 = _run(["git", "log", "--oneline", "-6"], timeout=20)
    sig["git_recent"] = s4.strip().splitlines()
    # 交易统计摘要(复用 self_tune 的证据)
    try:
        from supermarket.config import Config
        from supermarket.memory import AIMemory
        from supermarket.self_tune import collect_evidence
        cfg = Config.load()
        base = Path(cfg.state_path)
        mem = AIMemory(base / "live" if (base / "live").exists() else base)
        ev = collect_evidence(mem, recent_n=8)
        sig["trade_overview"] = ev.get("overview")
        sig["trade_groups"] = {k: v for k, v in (ev.get("groups") or {}).items()
                              if k in ("反转类型", "时段", "持有时间")}
    except Exception as e:
        sig["trade_overview"] = f"统计不可用: {str(e)[:80]}"
    return sig


def render_signals(sig: dict[str, Any]) -> str:
    L = ["# 系统信号(最近 24h)", ""]
    L.append(f"- 测试: {' | '.join(sig.get('tests_tail') or [])}")
    L.append(f"- 最近提交: {'; '.join((sig.get('git_recent') or [])[:5])}")
    ov = sig.get("trade_overview")
    if isinstance(ov, dict):
        L.append(f"- 交易统计: {ov.get('n')} 笔 胜率{ov.get('winrate')}% 平均{ov.get('avg')}")
        for k, v in (sig.get("trade_groups") or {}).items():
            L.append(f"    [{k}] " + ", ".join(f"{kk}:{vv['n']}笔/{vv['avg']}" for kk, vv in list(v.items())[:6]))
    L += ["", f"## 错误日志(去重, 共 {sig.get('error_total', 0)} 条)", ""]
    L += (sig.get("errors_unique") or ["(无 ERROR)"])
    L += ["", "## 服务最近日志", ""] + (sig.get("recent_lines") or [])
    L += ["", "## 项目结构", ""]
    L += ["```", _tree(), "```"]
    L += ["", "## 关键文件(供定位; 如需更多内容请基于 find 片段给出精确替换)", ""]
    L += ["```", _files_digest(), "```"]
    L += ["", "请输出 JSON(findings + edits)。没有真问题就给出空 edits。"]
    return "\n".join(L)


def _tree() -> str:
    code, s = _run(["bash", "-lc",
                    "find src scripts docs tests -maxdepth 2 -name '*.py' -o -maxdepth 2 -name '*.md' | sort"], timeout=30)
    return (s or "").strip()[:1500]


def _files_digest() -> str:
    """关键文件的函数签名摘要(省 token 又足够定位)。"""
    out: list[str] = []
    for f in sorted(Path("src/supermarket").glob("*.py")):
        rel = str(f)
        try:
            txt = f.read_text()
        except Exception:
            continue
        sigs = [ln.strip() for ln in txt.splitlines()
                if re.match(r"^\s*(def |class |[A-Z_]+:?\s*=)", ln) and "def _" not in ln[:6]]
        out.append(f"### {rel} ({len(txt.splitlines())} 行)")
        out.append("\n".join(sigs[:28]))
    return "\n".join(out)[:4000]


# ---------------- ③ AI 提案 ----------------
def parse_json(text: str) -> dict[str, Any]:
    from supermarket.self_tune import _extract_json
    cand = _extract_json(text or "")
    for t in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                obj.setdefault("edits", [])
                obj.setdefault("findings", [])
                return obj
        except Exception:
            continue
    return {"edits": [], "findings": [], "parse_error": "无法解析"}


def _read_slices(specs: list[str], budget: int = 28000) -> str:
    """按 "文件:起-止" 读取代码片段(带行号), 受总量预算限制。"""
    out: list[str] = []
    used = 0
    for sp in specs[:6]:
        m = re.match(r"^([\w./-]+)(?::(\d+)-(\d+))?$", str(sp).strip())
        if not m:
            out.append(f"(无法解析: {sp})")
            continue
        f, a, b = m.group(1), m.group(2), m.group(3)
        p = ROOT / f
        if not p.exists() or not str(p).startswith(str(ROOT)):
            out.append(f"(不存在: {f})")
            continue
        try:
            lines = p.read_text().splitlines()
        except Exception as e:
            out.append(f"(读取失败 {f}: {str(e)[:50]})")
            continue
        i = max(1, int(a)) if a else 1
        j = min(len(lines), int(b)) if b else min(len(lines), i + 120)
        seg = "\n".join(f"{k:5d}| {lines[k-1]}" for k in range(i, j + 1))
        if used + len(seg) > budget:
            seg = seg[: max(0, budget - used)]
        used += len(seg)
        out.append(f"### {f} [{i}-{j}]\n```python\n{seg}\n```")
        if used >= budget:
            break
    return "\n".join(out)


def ask_ai(cfg: Any, signals_text: str, retry_note: str = "") -> dict[str, Any]:
    import httpx
    hdr = {
        "Authorization": f"Bearer {cfg.llm.api_key}",
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "x-opencode-session": os.environ.get("LLM_SESSION", "evolve"),
    }
    msgs = [{"role": "system", "content": SYSTEM_EVOLVE.replace("{max_edits}", str(MAX_EDITS))},
            {"role": "user", "content": signals_text}]
    if retry_note:
        msgs.append({"role": "user", "content": retry_note})
    prop: dict[str, Any] = {}
    for round_i in range(3):                    # 探查最多 3 轮
        r = httpx.post(cfg.llm.base_url.rstrip("/") + "/chat/completions", headers=hdr,
                       json={"model": cfg.llm.model, "messages": msgs, "temperature": 0.2,
                             "max_tokens": int(getattr(cfg.llm, "max_tokens", 16000))},
                       timeout=300)
        r.raise_for_status()
        txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        msgs.append({"role": "assistant", "content": txt})
        prop = parse_json(txt)
        if prop.get("parse_error"):
            msgs.append({"role": "user", "content":
                         "上一次回复不是合法 JSON, 请严格只输出一行合法 JSON(字符串内不要出现英文双引号)。"})
            continue
        needs = prop.get("need_files") or []
        if needs and round_i < 2:
            LOG.info("代码自迭代: AI 请求查看 %s", needs[:4])
            msgs.append({"role": "user", "content":
                         "以下是你要的代码片段:\n" + _read_slices(needs)
                         + "\n现在请给出最终 JSON(findings + edits; 若确认无需改动就给空 edits)。"})
            continue
        break
    return prop


# ---------------- ④ 护栏 ----------------
def guard_edits(edits: list[dict]) -> tuple[list[dict], list[str]]:
    ok: list[dict] = []
    errs: list[str] = []
    if len(edits) > MAX_EDITS:
        errs.append(f"改动条数 {len(edits)} > {MAX_EDITS}")
        edits = edits[:MAX_EDITS]
    for e in edits:
        f = str(e.get("file", "")).strip()
        find = e.get("find") or ""
        rep = e.get("replace") or ""
        if not f or not find:
            errs.append("编辑项缺 file/find")
            continue
        if f in PROTECTED_FILES:
            errs.append(f"❌ 保护文件禁止自动改: {f}")
            continue
        if f.startswith("tests/") and not re.match(r"^tests/test_evolve", f):
            errs.append(f"❌ 既有测试禁止改: {f}")
            continue
        if not (f.startswith(ALLOWED_PREFIXES) or f in ALLOWED_EXACT):
            errs.append(f"❌ 白名单外路径: {f}")
            continue
        p = ROOT / f
        if not p.exists():
            errs.append(f"文件不存在: {f}")
            continue
        try:
            txt = p.read_text()
        except Exception as ex:
            errs.append(f"读取失败 {f}: {str(ex)[:60]}")
            continue
        n = txt.count(find)
        if n != 1:
            errs.append(f"find 在 {f} 中出现 {n} 次(必须唯一)")
            continue
        ok.append({"file": f, "find": find, "replace": rep, "why": str(e.get("why", ""))[:200]})
    return ok, errs


# ---------------- ⑤ 应用 / 测试闸门 / 上线 ----------------
def apply_and_verify(edits: list[dict]) -> tuple[bool, str]:
    """应用 → 语法检查 → pytest 全绿。失败立即 revert。"""
    originals: dict[str, str] = {}
    for e in edits:
        p = ROOT / e["file"]
        originals[e["file"]] = p.read_text()
        p.write_text(originals[e["file"]].replace(e["find"], e["replace"], 1))
    # 语法
    for e in edits:
        code, out = _run([".venv/bin/python", "-c",
                          f"import ast;ast.parse(open('{e['file']}').read())"], timeout=60)
        if code != 0:
            _restore(originals)
            return False, f"语法错误 {e['file']}: {out.strip()[:150]}"
    # 测试
    code, out = _run([".venv/bin/python", "-m", "pytest", "tests/", "-q"], timeout=1200)
    if code != 0:
        tail = "\n".join((out or "").strip().splitlines()[-8:])
        _restore(originals)
        return False, f"测试未通过:\n{tail}"
    return True, (out or "").strip().splitlines()[-1] if out else "ok"


def _restore(originals: dict[str, str]) -> None:
    for f, txt in originals.items():
        try:
            (ROOT / f).write_text(txt)
        except Exception:
            pass


def commit_and_deploy(edits: list[dict], findings: list[dict], test_line: str) -> str:
    """打 tag(改动前)→ 提交 → 推送 → 重启服务。"""
    tag = "evolve-" + time.strftime("%Y%m%d-%H%M")
    # tag 指向当前(改动已在工作树) → 先 stash 提交前状态? 简化: 用 HEAD 作回滚点
    _run(["git", "tag", "-f", tag], timeout=30)
    files = sorted({e["file"] for e in edits})
    _run(["git", "add", *files], timeout=60)
    msg = ("code-evolve: " + "; ".join(f"{f}" for f in files)
           + f" | findings={len(findings)} tests={test_line[:80]}")
    _run(["git", "-c", "user.email=hermes@local", "-c", "user.name=hermes",
          "commit", "-m", msg[:400]], timeout=120)
    _run(["git", "push", "origin", "master"], timeout=180)
    _run(["systemctl", "restart", "supermarket-live"], timeout=120)
    return tag


# ---------------- 主流程 ----------------
def run_evolve(cfg: Any, dry_run: bool = False) -> dict[str, Any]:
    res: dict[str, Any] = {"findings": [], "edits": [], "errors": [], "applied": False}
    # ① 健康检查(异常则回滚上一轮并停止本轮)
    h = check_health()
    res["health"] = h
    if not h["healthy"]:
        LOG.warning("健康检查不通过: %s", h["reasons"])
        if not dry_run and rollback_last_round("; ".join(h["reasons"])):
            res["rolled_back"] = True
            return res
    # ② 信号
    sig = collect_signals()
    text = render_signals(sig)
    # ③ AI 提案(解析失败重试一次)
    prop: dict[str, Any] = {}
    for attempt in range(2):
        try:
            prop = ask_ai(cfg, text,
                          retry_note=("上一次回复不是合法 JSON, 请严格只输出一行合法 JSON。"
                                      if attempt else ""))
        except Exception as e:
            LOG.error("代码自迭代 AI 调用失败: %s", str(e)[:150])
            res["errors"].append(f"AI 调用失败: {str(e)[:150]}")
            return res
        if not prop.get("parse_error"):
            break
    res["findings"] = prop.get("findings") or []
    res["proposal_edits"] = len(prop.get("edits") or [])
    if prop.get("parse_error"):
        res["errors"].append(f"解析失败: {prop['parse_error']}")
        return res
    # ④ 护栏
    edits, errs = guard_edits(prop.get("edits") or [])
    res["errors"] = errs
    res["edits"] = [{"file": e["file"], "why": e["why"]} for e in edits]
    if not edits:
        LOG.info("代码自迭代: 无需改动(findings=%d, 被拒=%d)", len(res["findings"]), len(errs))
        return res
    if dry_run:
        LOG.info("[dry-run] 拟改 %d 处: %s", len(edits), [e['file'] for e in edits])
        res["dry_run"] = True
        return res
    # ⑤ 应用 + 测试闸门
    ok, note = apply_and_verify(edits)
    if not ok:
        LOG.warning("代码自迭代: 改动未通过验证, 已还原 → %s", note[:200])
        res["errors"].append(note[:300])
        res["applied"] = False
        try:
            notify.risk_event(f"🧪 代码自迭代改动未通过闸门, 已自动还原: {note[:150]}")
        except Exception:
            pass
        return res
    # ⑥ 上线
    tag = commit_and_deploy(edits, res["findings"], note)
    res["applied"] = True
    res["tag"] = tag
    d = _load_log()
    d.setdefault("rounds", []).append({
        "ts": time.time(), "tag": tag, "edits": res["edits"],
        "findings": [f.get("what") for f in res["findings"]][:6], "tests": note[:120]})
    _save_log(d)
    LOG.warning("代码自迭代已上线(回滚点 %s): %s", tag, [e["file"] for e in edits])
    try:
        notify.risk_event("🛠 代码自迭代已上线(可回滚 " + tag + ")\n"
                          + "\n".join(f"· {e['file']}: {e['why'][:80]}" for e in res["edits"])
                          + f"\n测试: {note[:80]}")
    except Exception:
        pass
    return res


def main() -> None:
    import argparse
    from supermarket.config import Config
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    cfg = Config.load()
    out = run_evolve(cfg, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:2500])


if __name__ == "__main__":
    main()
