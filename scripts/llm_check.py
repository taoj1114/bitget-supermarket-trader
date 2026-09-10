#!/usr/bin/env python3
"""LLM 端点诊断: 验证 .env 的 OpenAI 兼容端点可调用。改自 nautilus-live。"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supermarket.config import Config  # noqa: E402


def _req(url: str, key: str, payload: dict | None = None, timeout: int = 45):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST" if payload else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main() -> None:
    cfg = Config.load("config.yaml")
    llm = cfg.llm
    print(f"端点: {llm.base_url or '(空)'}  模型: {llm.model}  key长度: {len(llm.api_key)}")
    if not llm.ready:
        print("[FAIL] LLM 未配置(填 .env 的 LLM_* 变量)")
        sys.exit(1)
    base = llm.base_url.rstrip("/")
    code, body = _req(f"{base}/chat/completions", llm.api_key, {
        "model": llm.model,
        "messages": [{"role": "user", "content": "回复OK"}],
        "max_tokens": 10, "stream": False,
    }, timeout=45)
    if code == 200:
        data = json.loads(body)
        print(f"[OK] chat 可调用: {data['choices'][0]['message']['content'][:40]!r}")
    else:
        print(f"[FAIL] chat 失败: HTTP {code} {body[:200]}")
        print("常见: 403=key过期/额度耗尽, 402=余额不足, 404=模型名/路径错")
        sys.exit(1)


if __name__ == "__main__":
    main()