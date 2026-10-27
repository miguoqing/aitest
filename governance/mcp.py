"""MCP 连接治理：先预检，再重试，最后给可执行的诊断。

MCP server 起不来时的常见形态是"工具列表为空"或"整轮卡住 30 秒后超时"，两种
都难定位。这里把失败拆成四个可区分的阶段：

    preflight   → 命令/工作目录/脚本/环境变量是否存在
    spawn       → 进程能否启动
    initialize  → JSON-RPC 握手（initialize）
    tools_list  → 能否列出工具

每个阶段失败都带 remediation hint。预检不过就不要起进程，起不来就不要反复
重试——先治配置，再谈连接。
"""

from __future__ import annotations

import json
import os
import pathlib
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .policy import McpPolicy, Policy

__all__ = ["McpServerSpec", "ProbeResult", "preflight", "handshake", "probe", "load_servers_from_dsh_config", "check_servers"]

_FILE_SUFFIXES = {".py", ".mjs", ".cjs", ".js", ".ts", ".sh"}


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    transport: str = "stdio"

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "McpServerSpec":
        args = config.get("args") or ()
        env = config.get("env") or {}
        return cls(
            name=str(config.get("serverName") or config.get("name") or "unnamed"),
            command=str(config.get("command") or ""),
            args=tuple(str(item) for item in args),
            env={str(key): ("" if value is None else str(value)) for key, value in dict(env).items()},
            cwd=str(config["cwd"]) if config.get("cwd") else None,
            transport=str(config.get("transport") or "stdio"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": self.command, "args": list(self.args), "cwd": self.cwd, "transport": self.transport}


@dataclass
class ProbeResult:
    server: str
    ok: bool
    stage: str
    detail: str = ""
    tools: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    attempts: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "ok": self.ok,
            "stage": self.stage,
            "detail": self.detail,
            "tools": self.tools,
            "hints": self.hints,
            "attempts": self.attempts,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }

    def summary(self) -> str:
        status = "OK" if self.ok else "FAIL"
        base = f"[{status}] {self.server} @ {self.stage}: {self.detail}"
        if self.tools:
            base += f"；工具 {len(self.tools)} 个: {', '.join(self.tools[:8])}"
        if self.hints:
            base += "\n  建议: " + "；".join(self.hints)
        return base


def preflight(spec: McpServerSpec) -> ProbeResult:
    hints: list[str] = []
    if spec.transport != "stdio":
        return ProbeResult(spec.name, True, "preflight", f"transport={spec.transport}，跳过本地进程预检")

    if not spec.command:
        return ProbeResult(spec.name, False, "preflight", "缺少 command", hints=["在 MCP 配置里补 command 绝对路径"])

    command_path = spec.command if os.path.isabs(spec.command) else shutil.which(spec.command)
    if not command_path or not os.path.exists(command_path):
        return ProbeResult(
            spec.name,
            False,
            "preflight",
            f"命令不存在: {spec.command}",
            hints=["用绝对路径，或确认 PATH 里能找到该命令", "Windows 下 npx 需要 .cmd 后缀"],
        )
    if os.path.isabs(spec.command) and not os.access(command_path, os.X_OK):
        return ProbeResult(spec.name, False, "preflight", f"命令不可执行: {command_path}", hints=["chmod +x 或改用解释器启动"])

    if spec.cwd and not os.path.isdir(spec.cwd):
        return ProbeResult(spec.name, False, "preflight", f"工作目录不存在: {spec.cwd}", hints=["把 cwd 改成真实存在的目录"])

    for arg in spec.args:
        candidate = pathlib.Path(arg)
        looks_like_file = candidate.is_absolute() or (arg.endswith(tuple(_FILE_SUFFIXES)) and "/" in arg)
        if looks_like_file and not candidate.exists():
            return ProbeResult(
                spec.name,
                False,
                "preflight",
                f"脚本不存在: {arg}",
                hints=["确认脚本路径；相对路径要按 cwd 解析"],
            )

    empty_env = [key for key, value in spec.env.items() if value == ""]
    if empty_env:
        hints.append(f"环境变量为空: {', '.join(empty_env)}（相关工具可能 401/鉴权失败）")

    return ProbeResult(spec.name, True, "preflight", "预检通过", hints=hints)


def handshake(
    spec: McpServerSpec,
    *,
    timeout_seconds: float = 20.0,
    rpc_timeout_seconds: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> ProbeResult:
    started = time.time()
    merged_env = dict(os.environ)
    merged_env.update({key: value for key, value in spec.env.items() if value != ""})
    if env:
        merged_env.update(env)

    try:
        process = subprocess.Popen(
            [spec.command, *spec.args],
            cwd=spec.cwd or None,
            env=merged_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(
            spec.name,
            False,
            "spawn",
            f"进程启动失败: {exc}",
            hints=["检查 command 是否可执行、cwd 是否存在"],
            elapsed_seconds=time.time() - started,
        )

    deadline = time.time() + timeout_seconds
    try:
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "governance-preflight", "version": "1.0"}}}
        _send(process, initialize)
        response, error = _await_response(process, 1, min(rpc_timeout_seconds, deadline - time.time()))
        if error or response is None:
            return ProbeResult(
                spec.name,
                False,
                "initialize",
                f"握手失败: {error or '超时'}",
                hints=[_stderr_hint(process)],
                elapsed_seconds=time.time() - started,
            )

        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response, error = _await_response(process, 2, min(rpc_timeout_seconds, max(0.1, deadline - time.time())))
        if error or response is None:
            return ProbeResult(
                spec.name,
                False,
                "tools_list",
                f"列出工具失败: {error or '超时'}",
                hints=[_stderr_hint(process)],
                elapsed_seconds=time.time() - started,
            )

        tools = [str(item.get("name")) for item in response.get("result", {}).get("tools", []) if isinstance(item, Mapping)]
        return ProbeResult(
            spec.name,
            True,
            "tools_list",
            f"连接正常，工具 {len(tools)} 个",
            tools=tools,
            elapsed_seconds=time.time() - started,
        )
    finally:
        _terminate(process)


def probe(
    spec: McpServerSpec,
    *,
    policy: McpPolicy | Policy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    mcp_policy: McpPolicy = policy.mcp if isinstance(policy, Policy) else (policy or McpPolicy())
    checked = preflight(spec)
    if not checked.ok:
        return checked

    attempts = max(1, mcp_policy.health_check_attempts)
    last = checked
    for attempt in range(1, attempts + 1):
        result = handshake(spec, timeout_seconds=mcp_policy.startup_timeout_seconds)
        result.attempts = attempt
        last = result
        if result.ok:
            result.hints = checked.hints + result.hints
            return result
        if attempt < attempts:
            sleep(mcp_policy.backoff_seconds * attempt)
    last.hints = checked.hints + last.hints
    return last


def check_servers(
    specs: list[McpServerSpec],
    *,
    policy: McpPolicy | Policy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ProbeResult]:
    return [probe(spec, policy=policy, sleep=sleep) for spec in specs]


# --------------------------------------------------------------------------
# 从 dsh 配置读取 MCP server（容忍 !!js 自定义标签）
# --------------------------------------------------------------------------
def _construct_js(loader: Any, node: Any) -> Any:
    if isinstance(node, _yaml_classes()["ScalarNode"]):
        return loader.construct_scalar(node)
    if isinstance(node, _yaml_classes()["SequenceNode"]):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


def _yaml_classes() -> dict[str, Any]:
    import yaml

    return {"ScalarNode": yaml.ScalarNode, "SequenceNode": yaml.SequenceNode, "MappingNode": yaml.MappingNode}


def load_servers_from_dsh_config(path: str | os.PathLike[str]) -> list[McpServerSpec]:
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("tag:yaml.org,2002:js", _construct_js)
    data = yaml.load(pathlib.Path(path).read_text(encoding="utf-8"), Loader=_Loader)
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层应为插件列表")
    servers: list[McpServerSpec] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        if item.get("name") != "@deepseek-ai/dsh-mcp-client":
            continue
        config = item.get("config") or {}
        if not isinstance(config, Mapping):
            continue
        servers.append(McpServerSpec.from_mapping(config))
    return servers


def _send(process: subprocess.Popen[str], payload: Mapping[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _await_response(
    process: subprocess.Popen[str],
    request_id: int,
    timeout: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """读一行 JSON-RPC 响应，跳过 notification；超时/进程退出返回错误。"""
    if timeout <= 0:
        return None, "超时"
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    end = time.time() + timeout
    try:
        while time.time() < end:
            remaining = end - time.time()
            if not selector.select(timeout=max(0.0, remaining)):
                break
            line = process.stdout.readline()
            if not line:
                return None, _stderr_hint(process)
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    return None, f"JSON-RPC 错误: {message['error']}"
                return message, None
        return None, "响应超时"
    finally:
        selector.close()


def _stderr_hint(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
    tail = ""
    if process.stderr is not None:
        try:
            tail = (process.stderr.read() or "").strip().splitlines()[-3:]
            tail = " | ".join(tail)
        except Exception:  # noqa: BLE001
            tail = ""
    code = process.returncode
    return f"进程退出码 {code}" + (f"，stderr: {tail}" if tail else "")


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
