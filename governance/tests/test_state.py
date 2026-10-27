import json

import pytest

from governance.state import CheckpointStore, CompensationLog, file_digest, new_run_id


def test_records_and_completed_steps(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    store.record("functional-points", "ok", artifacts=["01.yaml"])
    store.record("scenarios", "ok")
    store.record("test-points", "failed", error="boom")
    assert store.completed_steps() == ["functional-points", "scenarios"]
    assert store.last_success()["step"] == "scenarios"


def test_resume_point_points_at_first_missing(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    store.record("functional-points", "ok")
    store.record("scenarios", "ok")
    point = store.resume_point(["functional-points", "scenarios", "test-points", "cases"])
    assert point.last_completed == "scenarios"
    assert point.next_step == "test-points"
    assert point.can_resume is True
    assert point.missing == ["test-points", "cases"]


def test_resume_point_complete(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    for step in ("a", "b"):
        store.record(step, "ok")
    point = store.resume_point(["a", "b"])
    assert point.next_step is None
    assert point.can_resume is False


def test_corrupt_line_does_not_break_resume(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    store.record("a", "ok")
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write("{不是合法 JSON\n")
    store.record("b", "ok")
    assert store.completed_steps() == ["a", "b"]


def test_idempotent_action_runs_once(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    calls = []

    def action():
        calls.append(1)
        return {"files": ["x.yaml"]}

    first, executed_first = store.idempotent("publish:topic", action)
    second, executed_second = store.idempotent("publish:topic", action)
    assert executed_first is True
    assert executed_second is False
    assert first == second == {"files": ["x.yaml"]}
    assert calls == [1]

    # 新建 store 指向同一目录（模拟进程重启），幂等仍然生效
    reopened = CheckpointStore(tmp_path / "run")
    third, executed_third = reopened.idempotent("publish:topic", action)
    assert executed_third is False
    assert third == first


def test_run_step_records_failure_and_reraises(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    with pytest.raises(RuntimeError):
        with store.run_step("merge", shard="cases"):
            raise RuntimeError("merge failed")
    statuses = [item["status"] for item in store.records()]
    assert statuses == ["started", "failed"]


def test_run_step_records_success(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    with store.run_step("merge"):
        pass
    record = store.last_success("merge")
    assert record is not None
    assert record["status"] == "ok"


def test_compensation_rolls_back_lifo_and_tolerates_failure(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    log = CompensationLog(store=store)
    order = []

    log.register("write-file", lambda payload: order.append(("undo", payload)), "a.yaml")
    log.register("notify", lambda payload: order.append(("undo", payload)), "mail")

    def failing(_):
        raise RuntimeError("rollback failed")

    log.register("bad-step", failing, None)

    report = log.rollback()
    assert report["attempted"] == 3
    assert report["succeeded"] == 2
    assert len(report["failed"]) == 1
    assert order == [("undo", "mail"), ("undo", "a.yaml")]
    assert len(log) == 0
    rollback_records = [item for item in store.records() if item["step"] == "rollback"]
    assert rollback_records and rollback_records[-1]["status"] == "failed"
    assert rollback_records[-1]["failed"] == [{"name": "bad-step", "error": "RuntimeError: rollback failed"}]


def test_file_digest_changes_with_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one", encoding="utf-8")
    first = file_digest(path)
    path.write_text("two", encoding="utf-8")
    assert file_digest(path) != first


def test_new_run_id_unique():
    assert new_run_id() != new_run_id()
