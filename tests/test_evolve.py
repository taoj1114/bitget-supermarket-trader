"""代码自迭代(code_evolve)的硬护栏测试。

护栏(2026-09-29 用户: "让程序自己发现 bug/不合理/缺功能并自己改"):
  ① 保护清单文件永不自动改(签名接口/风控底线/TP-SL兜底/配置底线/自迭代自身)
  ② 既有测试文件永不改(防"改测试让自己通过")
  ③ 白名单外路径拒绝
  ④ find 必须在目标文件中唯一(防误替换)
  ⑤ 语法或测试不过 → 立即还原, 不留痕迹
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import supermarket.code_evolve as ce


def test_protected_files_are_rejected():
    for f in sorted(ce.PROTECTED_FILES):
        ok, errs = ce.guard_edits([{"file": f, "find": "x", "replace": "y"}])
        assert ok == [], f"{f} 必须被拒"
        assert any("保护文件" in e for e in errs), errs


def test_existing_tests_cannot_be_modified():
    ok, errs = ce.guard_edits([{"file": "tests/test_risk.py", "find": "assert", "replace": "assert True or"}])
    assert ok == [] and any("既有测试" in e for e in errs)
    # 新增 evolve 测试是允许的
    ok2, errs2 = ce.guard_edits([{"file": "tests/test_evolve_x.py", "find": "a", "replace": "b"}])
    assert all("既有测试" not in e for e in errs2)


def test_outside_whitelist_rejected():
    ok, errs = ce.guard_edits([{"file": "setup.py", "find": "a", "replace": "b"}])
    assert ok == [] and any("白名单" in e for e in errs)


def test_find_must_be_unique_and_exist():
    # 真实存在的文件里, 用一个不存在/不唯一的片段
    ok, errs = ce.guard_edits([{"file": "src/supermarket/indicators.py", "find": "import os", "replace": "import os2"}])
    assert ok == [] and any(("出现" in e) or ("不存在" in e) for e in errs)


def test_max_edits_capped():
    edits = [{"file": "src/supermarket/indicators.py", "find": f"zzz{i}", "replace": "x"}
             for i in range(ce.MAX_EDITS + 4)]
    ok, errs = ce.guard_edits(edits)
    assert any("改动条数" in e for e in errs)
    assert len(ok) <= ce.MAX_EDITS


def test_apply_and_verify_restores_on_syntax_error(monkeypatch=None):
    """语法错误必须还原文件(不留痕迹)。"""
    target = Path("src/supermarket/indicators.py")
    orig = target.read_text()
    # 构造一个会让语法崩掉的替换(真实片段)
    find = "import numpy as np"
    assert target.read_text().count(find) == 1
    ok, note = ce.apply_and_verify([{"file": str(target), "find": find,
                                     "replace": "import numpy as np\ndef broken(: pass"}])
    assert ok is False and "语法" in note
    assert target.read_text() == orig, "文件必须被还原"


def test_self_protection():
    """自迭代模块自身不可被自动改(防止自己给自己松绑)。"""
    for f in ("src/supermarket/code_evolve.py", "src/supermarket/self_tune.py"):
        assert f in ce.PROTECTED_FILES
