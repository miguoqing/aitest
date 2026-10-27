import pathlib

import pytest

from governance.policy import PolicyError, load_policy


def test_default_policy_matches_shipped_file():
    policy = load_policy()
    assert policy.loop.max_rounds == 30
    assert policy.loop.max_consecutive_failures == 2
    assert policy.retry.tool.max_attempts == 2
    assert policy.context.evidence_reserve_tokens < policy.context.max_tokens
    assert policy.safety.mode == "workspace-write"


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "nope.yaml")


def test_missing_default_file_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_GOVERNANCE_POLICY", raising=False)
    monkeypatch.setattr("governance.policy.DEFAULT_POLICY_PATH", tmp_path / "nope.yaml")
    policy = load_policy()
    assert policy.loop.max_rounds == 30


def test_overrides_merge_deeply():
    policy = load_policy(overrides={"loop": {"max_rounds": 5, "deadline_seconds": 60}})
    assert policy.loop.max_rounds == 5
    assert policy.loop.deadline_seconds == 60
    assert policy.loop.max_tool_calls == 120  # 未覆盖的沿用文件值


def test_unknown_field_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("loop:\n  max_roundz: 10\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="未知策略字段"):
        load_policy(path)


def test_invalid_value_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("loop:\n  duplicate_call_threshold: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="duplicate_call_threshold"):
        load_policy(path)


def test_negative_number_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("loop:\n  max_rounds: -3\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_json_policy_is_supported(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"loop": {"max_rounds": 7}}', encoding="utf-8")
    assert load_policy(path).loop.max_rounds == 7
