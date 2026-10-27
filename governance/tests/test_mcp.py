import pathlib
import sys

from governance.mcp import McpServerSpec, handshake, load_servers_from_dsh_config, preflight, probe

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "mcp_server.py"


def test_preflight_missing_command(tmp_path):
    result = preflight(McpServerSpec(name="bad", command="/definitely/not/here"))
    assert result.ok is False
    assert result.stage == "preflight"
    assert "命令不存在" in result.detail
    assert result.hints


def test_preflight_missing_script(tmp_path):
    spec = McpServerSpec(name="missing-script", command=sys.executable, args=(str(tmp_path / "nope.py"),))
    result = preflight(spec)
    assert result.ok is False
    assert "脚本不存在" in result.detail


def test_preflight_missing_cwd(tmp_path):
    spec = McpServerSpec(name="bad-cwd", command=sys.executable, cwd=str(tmp_path / "nope"))
    result = preflight(spec)
    assert result.ok is False
    assert "工作目录不存在" in result.detail


def test_preflight_ok_for_python_fixture():
    spec = McpServerSpec(name="tiny", command=sys.executable, args=(str(FIXTURE),))
    result = preflight(spec)
    assert result.ok is True
    assert result.stage == "preflight"


def test_preflight_warns_on_empty_env():
    spec = McpServerSpec(name="tiny", command=sys.executable, args=(str(FIXTURE),), env={"API_KEY": ""})
    result = preflight(spec)
    assert result.ok is True
    assert any("API_KEY" in hint for hint in result.hints)


def test_handshake_lists_tools():
    spec = McpServerSpec(name="tiny", command=sys.executable, args=(str(FIXTURE),))
    result = handshake(spec, timeout_seconds=15)
    assert result.ok is True, result.detail
    assert result.tools == ["ping"]


def test_handshake_reports_spawn_failure(tmp_path):
    spec = McpServerSpec(name="ghost", command=str(tmp_path / "ghost-binary"))
    result = handshake(spec, timeout_seconds=2)
    assert result.ok is False
    assert result.stage == "spawn"


def test_probe_short_circuits_on_preflight_failure():
    result = probe(McpServerSpec(name="bad", command="/definitely/not/here"))
    assert result.ok is False
    assert result.attempts == 0


def test_probe_reports_attempts():
    spec = McpServerSpec(name="tiny", command=sys.executable, args=(str(FIXTURE),))
    result = probe(spec, sleep=lambda _: None)
    assert result.ok is True
    assert result.attempts == 1


def test_load_servers_from_dsh_config(tmp_path):
    config = tmp_path / "dsh.yml"
    config.write_text(
        """
- id: mcp-a
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: tiny
    transport: stdio
    command: /usr/bin/env
    args:
      - node
      - /tmp/tiny.mjs
    env:
      FOO: !!js process.env.FOO ?? ''
- id: other-plugin
  name: '@deepseek-ai/dsh-tool-bash'
""",
        encoding="utf-8",
    )
    servers = load_servers_from_dsh_config(config)
    assert len(servers) == 1
    assert servers[0].name == "tiny"
    assert servers[0].args == ("node", "/tmp/tiny.mjs")
    assert "process.env.FOO" in servers[0].env["FOO"]
