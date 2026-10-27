import json
import pathlib

import pytest

from governance.cli import main
from governance.trace import (
    build_items,
    encode_cwd,
    filter_items,
    find_session,
    load_events,
    render_items,
    summarize,
)

EVENTS = [
    {"type": "session", "seq": 1, "time": 1000, "data": {"id": "s1", "cwd": "/x"}},
    {"type": "turn/start", "seq": 2, "time": 1100, "data": {"turn": 1}},
    {"type": "step/start", "seq": 3, "time": 1200, "data": {"turn": 1, "step": 1}},
    {"type": "user/message", "seq": 4, "time": 1300, "data": {"content": [{"type": "text", "text": "帮我生成用例"}]}},
    {
        "type": "assistant/message",
        "seq": 5,
        "time": 2000,
        "data": {
            "turn": 1,
            "step": 1,
            "message": {"content": [{"type": "text", "text": "好的"}, {"type": "tool-call", "id": "c1", "name": "bash"}]},
            "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 0},
        },
    },
    {"type": "tool/call", "seq": 6, "time": 2100, "data": {"turn": 1, "step": 1, "callId": "c1", "name": "bash", "arguments": '{"command": "ls"}'}},
    {
        "type": "tool/result",
        "seq": 7,
        "time": 2400,
        "data": {
            "turn": 1,
            "step": 1,
            "message": {"toolCallId": "c1", "content": [{"type": "text", "text": "a.md"}], "isError": False},
        },
    },
    {"type": "tool/call", "seq": 8, "time": 2500, "data": {"turn": 1, "step": 1, "callId": "c2", "name": "read", "arguments": '{"path": "nope"}'}},
    {
        "type": "tool/result",
        "seq": 9,
        "time": 2600,
        "data": {
            "turn": 1,
            "step": 1,
            "message": {"toolCallId": "c2", "content": [{"type": "text", "text": "Error: not found"}], "isError": True},
            "error": {"name": "FsError", "code": "ENOENT"},
        },
    },
    {"type": "turn/end", "seq": 10, "time": 3000, "data": {"turn": 1, "reason": "completed"}},
]


def write_session(tmp_path, events):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
    return path


def test_load_events_skips_partial_tail(tmp_path):
    path = write_session(tmp_path, EVENTS)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "tool/call", "seq": 11, "data": {')  # 写入中断
    events = load_events(path)
    assert [event["seq"] for event in events] == list(range(1, 11))


def test_tool_call_and_result_are_paired(tmp_path):
    items = build_items(load_events(write_session(tmp_path, EVENTS)))
    results = [item for item in items if item.kind == "tool_result"]
    assert [item.tool for item in results] == ["bash", "read"]
    assert results[0].status == "ok"
    assert results[0].duration_ms == 300
    assert results[1].status == "error"
    assert results[1].duration_ms == 100
    assert "ENOENT" in results[1].text


def test_render_is_readable(tmp_path):
    items = build_items(load_events(write_session(tmp_path, EVENTS)))
    text = "\n".join(render_items(items))
    assert "turn 1 开始" in text
    assert "step 1.1" in text
    assert "→ bash" in text
    assert "← 失败 ENOENT" in text
    assert "[10, 5, 0]" not in text


def test_summary_counts_turns_steps_tools_and_failures(tmp_path):
    items = build_items(load_events(write_session(tmp_path, EVENTS)))
    line = summarize(items)
    assert "turn 1" in line
    assert "step 1" in line
    assert "工具 2 次" in line
    assert "失败 1" in line
    assert "token in=10 out=5" in line


def test_errors_filter_keeps_call_and_result(tmp_path):
    items = build_items(load_events(write_session(tmp_path, EVENTS)))
    picked = filter_items(items, errors_only=True)
    kinds = [(item.kind, item.status) for item in picked]
    assert ("tool_result", "error") in kinds
    assert any(item.kind == "tool_call" and item.tool == "read" for item in picked)
    assert all(item.tool != "bash" for item in picked if item.kind == "tool_call")


def test_turn_filter(tmp_path):
    items = build_items(load_events(write_session(tmp_path, EVENTS)))
    picked = filter_items(items, turn=1)
    assert all(item.turn in (1, None) for item in picked)
    assert any(item.kind == "turn" for item in picked)


def test_encode_cwd_matches_dsh_layout(tmp_path):
    cwd = tmp_path / "project"
    expected = "-" + str(cwd.resolve()).replace("/", "-") + "--"
    assert encode_cwd(cwd) == expected
    assert expected.startswith("--") and expected.endswith("--")


def test_find_session_by_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    scope = tmp_path / "sessions" / encode_cwd("/work/demo")
    scope.mkdir(parents=True)
    target = scope / "session-demo.jsonl.zstd"
    target.write_bytes(b"")
    assert find_session(cwd="/work/demo") == target


def test_cli_trace_json(tmp_path, capsys):
    path = write_session(tmp_path, EVENTS)
    assert main(["trace", "--session", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(item["kind"] == "tool_result" for item in payload)


def test_cli_trace_missing_session_returns_two(tmp_path):
    assert main(["trace", "--session", str(tmp_path / "nope.jsonl")]) == 2
