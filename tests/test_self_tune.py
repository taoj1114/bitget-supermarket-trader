"""AI 自迭代(self_tune)的护栏测试: 提案解析/校验/白名单钳制/应用/自动回滚。

关键护栏(2026-09-29):
  ① 越界参数必须被拒(白名单范围)
  ② 非白名单参数必须被拒(风控底线不可被 AI 改)
  ③ 规则条数/长度上限
  ④ 变更可回滚(效果变差 → 自动恢复快照 + 记入"已证伪")
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import supermarket.self_tune as st


def _isolate(tmp: Path, monkeypatch=None):
    """把 self_tune 的输出文件指向临时目录。"""
    st.STATE_DIR = tmp
    st.RULES_FILE = tmp / "learned_rules.md"
    st.PARAMS_FILE = tmp / "tuned_params.json"
    st.LOG_FILE = tmp / "tuning_log.json"


def test_parse_proposal_tolerates_fences():
    txt = '```json\n{"analysis": "x", "changes": [{"kind":"param","name":"min_rr","value":1.2}]}\n```'
    p = st.parse_proposal(txt)
    assert p["analysis"] == "x" and len(p["changes"]) == 1
    assert not p.get("parse_error")


def test_validate_rejects_out_of_range_and_unknown_param():
    rules: list[str] = []
    ok, clean, errs = st.validate_proposal(
        {"changes": [
            {"kind": "param", "name": "time_stop_hours", "value": 36},       # 合法
            {"kind": "param", "name": "momentum_exit_floor", "value": 99},   # 越界
            {"kind": "param", "name": "min_rr", "value": 1.5},               # 非白名单(用户偏好锁定)
        ]}, rules, {})
    assert ok is True and len(clean) == 1 and clean[0]["name"] == "time_stop_hours"
    assert any("超出范围" in e for e in errs) and any("白名单" in e for e in errs)


def test_change_count_capped():
    """单次变更上限 3 条(护栏)。"""
    ch = [{"kind": "rule", "op": "add", "text": f"r{i}"} for i in range(5)]
    ok, clean, errs = st.validate_proposal({"changes": ch}, [], {})
    assert len(clean) <= st.MAX_CHANGES_PER_RUN
    assert any("上限" in e for e in errs)


def test_min_rr_locked_out_of_whitelist():
    """用户偏好锁定: TP 目标 2~3% + SL 1.5~2% → RR≈1.0, AI 不得提高 min_rr(否则卡死开仓)。"""
    assert "min_rr" not in st.ALLOWED_PARAMS
    ok, clean, errs = st.validate_proposal(
        {"changes": [{"kind": "param", "name": "min_rr", "value": 1.5}]}, [], {})
    assert ok is False and any("白名单" in e for e in errs)


def test_validate_rule_limits():
    rules = [f"规则{i}" for i in range(st.MAX_RULES)]
    ok, clean, errs = st.validate_proposal(
        {"changes": [{"kind": "rule", "op": "add", "text": "新规则"}]}, rules, {})
    assert ok is False and any("上限" in e for e in errs)
    long_text = "x" * (st.MAX_RULE_CHARS + 1)
    ok2, _, errs2 = st.validate_proposal(
        {"changes": [{"kind": "rule", "op": "add", "text": long_text}]}, [], {})
    assert ok2 is False and any("超长" in e for e in errs2)


def test_apply_and_rollback_restores_snapshot():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _isolate(tmp)
        # 初始: 一条规则 + 一个参数
        st.save_rules(["基线规则"])
        st.save_params({"momentum_exit_floor": 1.5})
        ev = {"total": 0, "overview": {"avg": 0.10}}   # 基线 0 笔 → 变更后 12 笔全部用于评估
        entry = st.apply_changes([
            {"kind": "rule", "op": "add", "text": "新经验: 盘前不开多", "evidence": "pre_market 9笔 -1.01"},
            {"kind": "param", "name": "momentum_exit_floor", "value": 2.0, "evidence": "6-24h 组平均为负"},
        ], ev)
        assert entry["version"] == 1
        assert "新经验: 盘前不开多" in st.load_rules()
        assert st.load_params()["momentum_exit_floor"] == 2.0

        # 变更后跑得很差 → 自动回滚
        class FakeMem:
            decisions = [{"outcome": "closed", "pnl": -0.5, "ts": 1, "close_ts": 2}] * 12
        rolled = st.evaluate_and_maybe_rollback(FakeMem())
        assert rolled is not None and rolled.get("rolled_back") is True
        assert "新经验: 盘前不开多" not in st.load_rules(), "应恢复到变更前快照"
        assert st.load_params()["momentum_exit_floor"] == 1.5
        log = json.loads(st.LOG_FILE.read_text())
        assert log["falsified"], "回滚应记入已证伪(防止重复提同一调整)"


def test_evaluate_keeps_when_better():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _isolate(tmp)
        st.save_rules([])
        st.save_params({})
        st.apply_changes([{"kind": "rule", "op": "add", "text": "好规则", "evidence": "x"}],
                         {"total": 0, "overview": {"avg": -0.1}})

        class FakeMem:
            decisions = [{"outcome": "closed", "pnl": 0.2, "ts": 1, "close_ts": 2}] * 12
        kept = st.evaluate_and_maybe_rollback(FakeMem())
        assert kept is not None and kept.get("eval", {}).get("verdict") == "keep"
        assert "好规则" in st.load_rules(), "效果更好应保留"


def test_apply_tuned_overrides_clamps_and_whitelists():
    class Cfg:
        momentum_exit_floor = 1.5
        time_stop_hours = 48
        secret_param = "SHOULD_NOT_CHANGE"

    with tempfile.TemporaryDirectory() as td:
        _isolate(Path(td))
        st.save_params({"momentum_exit_floor": 2.25, "time_stop_hours": 36, "secret_param": 99})
        cfg = Cfg()
        cfg.time_stop_hours = 48
        applied = st.apply_tuned_overrides(cfg)
        assert cfg.momentum_exit_floor == 2.25 and cfg.time_stop_hours == 36
        assert cfg.secret_param == "SHOULD_NOT_CHANGE", "非白名单字段永不覆盖"
        assert set(applied) == {"momentum_exit_floor", "time_stop_hours"}


def test_param_unchanged_is_skipped():
    rules: list[str] = []
    ok, clean, errs = st.validate_proposal(
        {"changes": [{"kind": "param", "name": "time_stop_hours", "value": 48}]},
        rules, {"time_stop_hours": 48})
    assert ok is False and clean == []


def test_evidence_required_fields_present():
    ok, clean, _ = st.validate_proposal(
        {"changes": [{"kind": "rule", "op": "add", "text": "r",
                      "evidence": "regular 16笔 37.5%"}]}, [], {})
    assert ok and clean[0]["evidence"]
