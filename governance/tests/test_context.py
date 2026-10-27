from governance.context import ContextGovernor, estimate_tokens
from governance.policy import ContextPolicy


def make_governor(**overrides):
    defaults = dict(
        max_tokens=1000,
        keep_recent_turns=2,
        tool_result_max_chars=400,
        tool_result_head_chars=240,
        tool_result_tail_chars=80,
        evidence_reserve_tokens=200,
    )
    defaults.update(overrides)
    return ContextGovernor(ContextPolicy(**defaults))


def test_estimate_tokens_counts_cjk_higher():
    assert estimate_tokens("测试用例") > estimate_tokens("test")
    assert estimate_tokens("") == 0


def test_large_tool_result_is_truncated_with_marker():
    governor = make_governor()
    huge = "头" * 500 + "尾" * 500
    governor.add_tool_result("read_file", huge)
    rendered = governor.render()
    content = rendered[0]["content"]
    assert "省略" in content
    assert len(content) < len(huge)


def test_identical_tool_results_are_deduped():
    governor = make_governor(max_tokens=100000)
    governor.add("user", "读文件")
    governor.add_tool_result("read_file", "SAME-CONTENT" * 10)
    governor.add("assistant", "再读一次")
    governor.add_tool_result("read_file", "SAME-CONTENT" * 10)
    rendered = governor.render()
    joined = "\n".join(item["content"] for item in rendered)
    assert joined.count("SAME-CONTENT") == 10
    assert governor.last_stats.deduped == 1


def test_pinned_evidence_survives_budget_pressure():
    governor = make_governor(max_tokens=500, evidence_reserve_tokens=150, keep_recent_turns=1)
    governor.add_evidence("打卡范围 100 至 300 米为有效区间", "attendance-rule-configuration.md")
    for index in range(40):
        governor.add("user", f"第 {index} 轮：一些不重要的历史上下文 " + "x" * 80)
    rendered = governor.render()
    joined = "\n".join(item["content"] for item in rendered)
    assert "100 至 300 米" in joined
    assert governor.last_stats.pinned_kept == 1
    assert governor.last_stats.dropped > 0


def test_budget_is_respected_for_unpinned_messages():
    governor = make_governor(max_tokens=400, evidence_reserve_tokens=100, keep_recent_turns=2)
    for index in range(30):
        governor.add("assistant", f"消息 {index} " + "y" * 100)
    governor.render()
    assert governor.last_stats.tokens_out <= 400
    assert governor.last_stats.messages_out < 30


def test_recent_turns_are_kept():
    governor = make_governor(max_tokens=300, keep_recent_turns=3, evidence_reserve_tokens=50)
    for index in range(20):
        governor.add("assistant", f"旧消息 {index}")
    governor.add("assistant", "最新结论 A")
    rendered = governor.render()
    assert rendered[-1]["content"] == "最新结论 A"


def test_summary_note_explains_dropped_history():
    governor = make_governor(max_tokens=300, keep_recent_turns=1, evidence_reserve_tokens=50)
    for index in range(30):
        governor.add("user", "u" * 200)
    rendered = governor.render()
    assert rendered[0]["role"] == "system"
    assert "上下文治理" in rendered[0]["content"]


def test_report_and_reset():
    governor = make_governor()
    governor.add("user", "hi")
    governor.render()
    assert "上下文" in governor.report()
    governor.reset()
    assert len(governor) == 0
