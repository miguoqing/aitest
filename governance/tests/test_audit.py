import json

from governance.audit import audit_project, findings_to_json, format_findings, max_severity
from governance.cli import main
from governance.policy import load_policy


def make_project(tmp_path, *, with_governance=True, with_agents=True, bad_mcp=True):
    if with_governance:
        (tmp_path / "governance").mkdir()
        (tmp_path / "governance" / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    if with_agents:
        (tmp_path / "AGENTS.md").write_text(
            "# 规则\n- 轮次上限 30\n- 连续失败 2 次停止\n- 高风险操作需审批\n- 治理策略见 governance/policy.yaml\n",
            encoding="utf-8",
        )
    config = tmp_path / "dsh-web-config.yml"
    command = "/definitely/not/here/npx" if bad_mcp else "/usr/bin/env"
    config.write_text(
        f"""
- id: agent-loop
  name: '@deepseek-ai/dsh-agent-loop'
  config: {{}}
- id: bash-sandbox
  name: '@deepseek-ai/dsh-bash-sandbox'
  config:
    timeoutMs: 600000
- id: tool-result-pruner
  name: '@deepseek-ai/dsh-compaction-tool-result-pruner'
  config:
    thresholdChars: 8192
  disabled: true
- id: mcp-bad
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bad
    transport: stdio
    command: {command}
    args: []
    env:
      API_KEY: !!js process.env.DOES_NOT_EXIST_KEY ?? ''
""",
        encoding="utf-8",
    )
    return tmp_path


def test_audit_reports_mcp_preflight_failure(tmp_path):
    project = make_project(tmp_path)
    findings = audit_project(project, policy=load_policy(), patch_path=None)
    codes = {item.code for item in findings}
    assert "C3.mcp-preflight-failed" in codes
    assert max_severity(findings) == "high"


def test_audit_reports_context_and_loop_issues(tmp_path):
    project = make_project(tmp_path, bad_mcp=False)
    findings = audit_project(project, policy=load_policy(), patch_path=None)
    codes = {item.code for item in findings}
    assert "C5.pruner-disabled" in codes
    assert "C1.long-bash-timeout" in codes
    assert "C2.no-timeout-policy" in codes


def test_audit_flags_missing_agents_md(tmp_path):
    project = make_project(tmp_path, with_agents=False, bad_mcp=False)
    codes = {item.code for item in audit_project(project, policy=load_policy(), patch_path=None)}
    assert "C1.no-agents-md" in codes


def test_audit_flags_missing_policy(tmp_path):
    project = make_project(tmp_path, with_governance=False, bad_mcp=False)
    codes = {item.code for item in audit_project(project, policy=load_policy(), patch_path=None)}
    assert "C1.no-policy" in codes


def test_audit_is_clean_for_complete_fixture(tmp_path):
    project = make_project(tmp_path, bad_mcp=False)
    (project / "dsh-web-config.yml").write_text(
        """
- id: timeout-policy
  name: '@deepseek-ai/dsh-tool-call-timeout-policy'
- id: repeat-tool-reminder
  name: '@deepseek-ai/dsh-repeat-tool-reminder'
- id: loop-guard
  name: /abs/path/governance/hooks/dsh-loop-guard.mjs
""",
        encoding="utf-8",
    )
    findings = audit_project(project, policy=load_policy(), patch_path=None)
    assert [item.code for item in findings] == []
    assert max_severity(findings) == "info"
    assert "未发现配置级问题" in format_findings(findings)


def test_audit_flags_missing_runtime_hook(tmp_path):
    project = make_project(tmp_path, bad_mcp=False)
    (project / "dsh-web-config.yml").write_text(
        """
- id: timeout-policy
  name: '@deepseek-ai/dsh-tool-call-timeout-policy'
- id: repeat-tool-reminder
  name: '@deepseek-ai/dsh-repeat-tool-reminder'
""",
        encoding="utf-8",
    )
    codes = {item.code for item in audit_project(project, policy=load_policy(), patch_path=None)}
    assert "C1.no-runtime-hook" in codes


def test_format_and_json_renderers(tmp_path):
    findings = audit_project(make_project(tmp_path), policy=load_policy(), patch_path=None)
    text = format_findings(findings, tmp_path)
    assert "汇总" in text
    assert "改法" in text
    payload = json.loads(findings_to_json(findings))
    assert payload and {"category", "severity", "code", "fix"} <= set(payload[0])


def test_cli_fail_on_high_returns_one(tmp_path, capsys):
    project = make_project(tmp_path)
    assert main(["audit", "--root", str(project), "--fail-on", "high"]) == 1
    assert "C3.mcp-preflight-failed" in capsys.readouterr().out


def test_cli_fail_on_none_returns_zero(tmp_path):
    project = make_project(tmp_path)
    assert main(["audit", "--root", str(project), "--fail-on", "none"]) == 0
