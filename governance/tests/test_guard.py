import pytest

from governance.guard import LoopGuard, LoopHalted, fingerprint
from governance.policy import LoopPolicy


def make_guard(**overrides):
    return LoopGuard(policy=LoopPolicy(**overrides))


def test_max_rounds_blocks_next_round():
    guard = make_guard(max_rounds=2)
    assert guard.begin_round() == 1
    assert guard.begin_round() == 2
    with pytest.raises(LoopHalted) as excinfo:
        guard.begin_round()
    assert excinfo.value.reason == "max_rounds"
    assert guard.halted is True


def test_no_progress_halts_after_threshold():
    guard = make_guard(no_progress_rounds=2, max_rounds=10)
    guard.begin_round()
    guard.end_round()
    guard.begin_round()
    with pytest.raises(LoopHalted) as excinfo:
        guard.end_round()
    assert excinfo.value.reason == "no_progress"


def test_progress_resets_no_progress_counter():
    guard = make_guard(no_progress_rounds=2, max_rounds=10)
    guard.begin_round()
    guard.end_round()                      # 空转 1
    assert guard.no_progress_rounds == 1
    guard.begin_round()
    guard.mark_progress("artifact-1")      # 有新信息 → 归零
    guard.end_round()
    assert guard.no_progress_rounds == 0
    assert guard.halted is False


def test_duplicate_call_is_cut_at_threshold():
    guard = make_guard(duplicate_call_threshold=3, max_rounds=10)
    guard.begin_round()
    guard.record_tool_call("read", {"path": "wiki/index.md"})
    guard.record_tool_call("read", {"path": "wiki/index.md"})
    with pytest.raises(LoopHalted) as excinfo:
        guard.record_tool_call("read", {"path": "wiki/index.md"})
    assert excinfo.value.reason == "duplicate_call"


def test_different_arguments_are_not_duplicates():
    guard = make_guard(duplicate_call_threshold=2, max_rounds=10)
    guard.begin_round()
    guard.record_tool_call("read", {"path": "a.md"})
    guard.record_tool_call("read", {"path": "b.md"})
    assert guard.tool_calls == 2


def test_consecutive_failures_halt_with_reason():
    guard = make_guard(max_consecutive_failures=2, max_rounds=10)
    guard.begin_round()
    guard.record_tool_call("bash", {"command": "false"}, ok=False)
    with pytest.raises(LoopHalted) as excinfo:
        guard.record_tool_call("bash", {"command": "false2"}, ok=False)
    assert excinfo.value.reason == "consecutive_failures"
    assert "连续失败 2 次" in excinfo.value.detail


def test_success_resets_failure_streak():
    guard = make_guard(max_consecutive_failures=2, max_rounds=10)
    guard.begin_round()
    guard.record_tool_call("bash", {"command": "a"}, ok=False)
    guard.record_tool_call("bash", {"command": "b"}, ok=True)
    guard.record_tool_call("bash", {"command": "c"}, ok=False)
    assert guard.consecutive_failures == 1
    assert guard.halted is False


def test_deadline_is_enforced():
    now = [0.0]
    guard = LoopGuard(policy=LoopPolicy(deadline_seconds=10), clock=lambda: now[0])
    guard.begin_round()
    now[0] = 11.0
    with pytest.raises(LoopHalted) as excinfo:
        guard.begin_round()
    assert excinfo.value.reason == "deadline"


def test_tool_call_budget_is_enforced():
    guard = make_guard(max_tool_calls=2, max_rounds=10, duplicate_call_threshold=99)
    guard.begin_round()
    guard.record_tool_call("a")
    guard.record_tool_call("b")
    with pytest.raises(LoopHalted) as excinfo:
        guard.record_tool_call("c")
    assert excinfo.value.reason == "max_tool_calls"


def test_halted_guard_refuses_further_rounds():
    guard = make_guard(max_rounds=1)
    guard.begin_round()
    with pytest.raises(LoopHalted):
        guard.begin_round()
    with pytest.raises(LoopHalted) as excinfo:
        guard.begin_round()
    assert excinfo.value.reason == "already_halted"


def test_snapshot_and_report_are_serializable():
    guard = make_guard()
    guard.begin_round()
    guard.record_tool_call("read", {"path": "x"})
    guard.mark_progress("new-fact")
    guard.end_round()
    snapshot = guard.snapshot()
    assert snapshot["round"] == 1
    assert snapshot["unique_calls"] == 1
    assert "轮次 1/30" in guard.report()


def test_fingerprint_is_stable_across_key_order():
    assert fingerprint("t", {"a": 1, "b": 2}) == fingerprint("t", {"b": 2, "a": 1})
    assert fingerprint("t", {"a": 1}) != fingerprint("t", {"a": 2})
