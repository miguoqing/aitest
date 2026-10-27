"""工程审计：按 7 类循环故障扫描配置与资产，输出可执行的问题清单。

审计不追求"零告警"，追求**每条告警都能指出改哪里**。所以每个 finding 都带
category（对应七类故障）、severity、文件位置和修复建议；CI 里用
`--fail-on high` 只拦会真正把循环跑死的配置。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .policy import Policy, load_policy

__all__ = ["Finding", "CATEGORIES", "audit_project", "format_findings", "max_severity"]

CATEGORIES: dict[str, str] = {
    "loop": "1. 循环失控",
    "tool": "2. 工具调用失败",
    "mcp": "3. MCP 连接/权限",
    "state": "4. 状态漂移",
    "context": "5. 上下文污染",
    "cost": "6. 成本不可控",
    "safety": "7. 安全风险",
}

_SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}
_SEVERITY_LABEL = {"high": "HIGH", "medium": "MED", "low": "LOW", "info": "INFO"}

_DANGEROUS_SNIPPETS = (
    ("rm -rf /", "删除根目录"),
    ("git push --force", "强推远端"),
    ("git reset --hard", "丢弃本地改动"),
    ("drop table", "删表"),
    ("chmod 777", "放开权限"),
)


@dataclass(frozen=True)
class Finding:
    category: str
    severity: str
    code: str
    title: str
    detail: str = ""
    file: str = ""
    line: int | None = None
    fix: str = ""

    def sort_key(self) -> tuple[int, str, str]:
        return (-_SEVERITY_ORDER[self.severity], self.category, self.code)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def format(self, root: pathlib.Path | None = None) -> str:
        location = ""
        if self.file:
            path = self.file
            if root is not None:
                try:
                    path = str(pathlib.Path(self.file).relative_to(root))
                except ValueError:
                    path = self.file
            location = f" ({path}{':' + str(self.line) if self.line else ''})"
        text = f"[{_SEVERITY_LABEL[self.severity]}] {self.code}{location} {self.title}"
        if self.detail:
            text += f"\n       现状: {self.detail}"
        if self.fix:
            text += f"\n       改法: {self.fix}"
        return text


def max_severity(findings: Iterable[Finding]) -> str:
    worst = "info"
    for finding in findings:
        if _SEVERITY_ORDER[finding.severity] > _SEVERITY_ORDER[worst]:
            worst = finding.severity
    return worst


def _finding_line(path: pathlib.Path, needle: str) -> int | None:
    try:
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in line:
                return index
    except (OSError, UnicodeDecodeError):
        return None
    return None


def audit_project(
    root: str | os.PathLike[str],
    *,
    policy: Policy | None = None,
    patch_path: str | os.PathLike[str] | None | str = "auto",
) -> list[Finding]:
    """扫描工程。

    patch_path="auto" 时读取 ~/.dsh/profiles/web/cordis.patch.yml 作为权威覆盖层；
    传 None 可跳过（测试里避免误读本机 profile）。
    """
    root_path = pathlib.Path(root).resolve()
    policy = policy or load_policy()
    findings: list[Finding] = []

    findings.extend(_check_governance_assets(root_path))
    findings.extend(_check_agents_instructions(root_path))
    findings.extend(_check_dsh_config(root_path, policy, patch_path))
    findings.extend(_check_scripts(root_path, policy))
    findings.extend(_check_skill_budgets(root_path, policy))
    return sorted(findings, key=lambda item: item.sort_key())


# -- 1. 策略资产 ------------------------------------------------------------
def _check_governance_assets(root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    policy_path = root / "governance" / "policy.yaml"
    if not policy_path.exists():
        findings.append(
            Finding(
                "loop",
                "high",
                "C1.no-policy",
                "缺少统一的循环策略文件",
                "阈值散落在提示词里，改一处漏一处，无法被脚本校验",
                str(policy_path),
                fix="新增 governance/policy.yaml，集中定义轮次/重试/预算/风险阈值",
            )
        )
    return findings


# -- 2. 提示词接入 ----------------------------------------------------------
def _check_agents_instructions(root: pathlib.Path) -> list[Finding]:
    agents = root / "AGENTS.md"
    if not agents.exists():
        return [
            Finding(
                "loop",
                "medium",
                "C1.no-agents-md",
                "缺少 AGENTS.md",
                "模型每一轮看不到项目级纪律",
                str(agents),
                fix="新增 AGENTS.md，写明轮次上限、连续失败阈值、审批要求",
            )
        ]

    text = agents.read_text(encoding="utf-8")
    findings: list[Finding] = []
    required = {
        "轮次": "未写明轮次上限",
        "失败": "未写明连续失败阈值",
        "审批": "未写明高风险操作审批",
        "治理": "未引用 governance 策略（模型不会去读阈值）",
    }
    for needle, problem in required.items():
        if needle not in text:
            findings.append(
                Finding(
                    "loop" if needle != "审批" else "safety",
                    "medium" if needle != "治理" else "low",
                    "C1.agents-md-gap",
                    problem,
                    f"AGENTS.md 中找不到关键词 {needle!r}",
                    str(agents),
                    fix=f"在 AGENTS.md 的循环纪律一节补上 {needle} 相关条款，并指向 governance/policy.yaml",
                )
            )
    return findings


# -- 3. DSH 配置 ------------------------------------------------------------
def _load_dsh_config(path: pathlib.Path) -> list[dict[str, Any]] | None:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return None

    class _Loader(yaml.SafeLoader):
        pass

    def _construct_js(loader: Any, node: Any) -> Any:
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _Loader.add_constructor("tag:yaml.org,2002:js", _construct_js)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, list) else None


def _plugins(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("id"):
            result[str(entry["id"])] = dict(entry)
    return result


def _check_dsh_config(
    root: pathlib.Path,
    policy: Policy,
    patch_path: str | os.PathLike[str] | None | str = "auto",
) -> list[Finding]:
    candidates = [root / "dsh-web-config.yml", pathlib.Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"]
    config = next((path for path in candidates if path.exists()), None)
    if config is None:
        return [
            Finding(
                "mcp",
                "low",
                "C3.no-dsh-config",
                "未找到 DSH 运行时配置",
                "无法校验 MCP、超时与权限；如果本机不跑 DSH 可忽略",
                str(root / "dsh-web-config.yml"),
            )
        ]

    entries = _load_dsh_config(config)
    if entries is not None:
        if patch_path == "auto":
            patch_file = pathlib.Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
        else:
            patch_file = pathlib.Path(patch_path) if patch_path is not None else None
        if patch_file is not None and patch_file.exists() and patch_file.resolve() != config.resolve():
            patch_entries = _load_dsh_config(patch_file)
            if patch_entries is not None:
                # patch 层是权威覆盖：用 id 合并 config/disabled，再按 insert 追加
                entries = _merge_patch(entries, patch_entries)
    if entries is None:
        return [
            Finding(
                "mcp",
                "medium",
                "C3.config-unparsable",
                "DSH 配置无法解析",
                "自定义标签或语法错误会让整个 profile 加载失败",
                str(config),
                fix="用 dsh 自己的配置校验命令重新生成，不要手改生成产物",
            )
        ]

    plugins = _plugins(entries)
    findings: list[Finding] = []

    has_hook = any(
        "dsh-loop-guard" in str(entry.get("name", "")) or entry.get("id") == "loop-guard"
        for entry in entries
        if isinstance(entry, Mapping)
    )
    if not has_hook:
        findings.append(
            Finding(
                "loop",
                "medium",
                "C1.no-runtime-hook",
                "护栏未挂到运行时执行路径",
                "提示词与脚本门禁只能事后发现问题；工具管线上的 deny/block 才能当场拦住",
                str(config),
                fix="把 governance/hooks/dsh-loop-guard.mjs 按 governance/dsh/README.md 挂到 cordis.patch.yml",
            )
        )

    loop = plugins.get("agent-loop")
    if loop:
        findings.append(
            Finding(
                "loop",
                "info",
                "C1.no-round-limit",
                "agent-loop 本身没有轮次上限配置项",
                "该插件只提供 maxParallelToolCalls（默认 10），轮次/空转/重复调用必须在编排层约束",
                str(config),
                _finding_line(config, "id: agent-loop"),
                fix="由 governance.LoopGuard 包住 while 循环；阈值见 governance/policy.yaml",
            )
        )

    bash_sandbox = plugins.get("bash-sandbox")
    if bash_sandbox:
        timeout_ms = (bash_sandbox.get("config") or {}).get("timeoutMs")
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 300_000:
            findings.append(
                Finding(
                    "loop",
                    "medium",
                    "C1.long-bash-timeout",
                    "单条 shell 超时过长",
                    f"timeoutMs={timeout_ms}；一条挂起的命令会吞掉整轮预算",
                    str(config),
                    _finding_line(config, "bash-sandbox"),
                    fix="降到 300000ms 以内，长任务改后台 job + 轮询",
                )
            )

    if "repeat-tool-reminder" not in plugins:
        findings.append(
            Finding(
                "loop",
                "low",
                "C1.no-repeat-reminder",
                "未启用重复工具调用提醒",
                "模型重复读同一文件时没有任何提示",
                str(config),
                fix="启用 @deepseek-ai/dsh-repeat-tool-reminder，阈值 3/5/8",
            )
        )

    if "tool-call-timeout-policy" not in plugins and "timeout-policy" not in plugins:
        findings.append(
            Finding(
                "tool",
                "medium",
                "C2.no-timeout-policy",
                "未启用工具调用超时策略",
                "单个 MCP 工具卡住会拖死整轮",
                str(config),
                fix="启用 @deepseek-ai/dsh-tool-call-timeout-policy",
            )
        )

    pruner = plugins.get("tool-result-pruner")
    if pruner and pruner.get("disabled") is True:
        findings.append(
            Finding(
                "context",
                "medium",
                "C5.pruner-disabled",
                "工具结果裁剪被禁用",
                "长循环里单条大输出会持续占用上下文，直到触发整体压缩",
                str(config),
                _finding_line(config, "tool-result-pruner"),
                fix="恢复 tool-result-pruner，thresholdChars 建议 8192",
            )
        )

    spill = plugins.get("spill-policy")
    if spill:
        max_inline = (spill.get("config") or {}).get("maxInlineTokens")
        if isinstance(max_inline, int) and max_inline > 50_000:
            findings.append(
                Finding(
                    "context",
                    "medium",
                    "C5.inline-too-large",
                    "单次工具结果内联上限过大",
                    f"maxInlineTokens={max_inline}",
                    str(config),
                    fix="降到 12500–20000，超出部分落盘引用",
                )
            )

    approval = plugins.get("approval")
    approval_policy = ((approval or {}).get("config") or {}).get("policy")
    if isinstance(approval_policy, str) and "never" in approval_policy:
        findings.append(
            Finding(
                "safety",
                "low",
                "C7.approval-conditional",
                "审批策略随环境变量变化",
                "DSH_PERMISSION_MODE=danger-full-access 时会变成 never",
                str(config),
                _finding_line(config, "id: approval"),
                fix="日常使用固定 workspace-write + ask；完全访问只用于隔离容器",
            )
        )

    findings.extend(_check_mcp_servers(config, entries, policy))
    return findings


def _merge_patch(entries: list[dict[str, Any]], patch: list[Any]) -> list[dict[str, Any]]:
    """把 profile patch 层合并进生成配置：同 id 覆盖 config/disabled，insert 追加。"""
    merged = [dict(entry) if isinstance(entry, Mapping) else entry for entry in entries]
    index = {entry.get("id"): entry for entry in merged if isinstance(entry, Mapping)}
    for item in patch:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("insert"), list):
            for added in item["insert"]:
                if not isinstance(added, Mapping):
                    continue
                existing = index.get(added.get("id"))
                if existing is not None:
                    # 生成配置已包含该 insert：按覆盖处理，避免同一 server 被算两遍
                    _apply_entry(existing, added)
                else:
                    merged.append(dict(added))
                    index[added.get("id")] = merged[-1]
            continue
        target = index.get(item.get("id"))
        if target is not None:
            _apply_entry(target, item)
    return merged


def _apply_entry(target: dict[str, Any], override: Mapping[str, Any]) -> None:
    if "disabled" in override:
        target["disabled"] = override["disabled"]
    if isinstance(override.get("config"), Mapping):
        target["config"] = {**(target.get("config") or {}), **override["config"]}


def _check_mcp_servers(config: pathlib.Path, entries: list[dict[str, Any]], policy: Policy) -> list[Finding]:
    from .mcp import McpServerSpec, preflight

    findings: list[Finding] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("name") != "@deepseek-ai/dsh-mcp-client":
            continue
        spec = McpServerSpec.from_mapping(entry.get("config") or {})
        result = preflight(spec)
        line = _finding_line(config, f"serverName: {spec.name}")
        if not result.ok:
            findings.append(
                Finding(
                    "mcp",
                    "high",
                    "C3.mcp-preflight-failed",
                    f"MCP server {spec.name} 预检失败",
                    result.detail,
                    str(config),
                    line,
                    fix="；".join(result.hints) or "修正 command/cwd/脚本路径",
                )
            )
        for key, value in spec.env.items():
            env_refs = re.findall(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)", value)
            effective = "" if value == "" else (os.environ.get(env_refs[0], "") if env_refs and "?? ''" in value else value)
            if effective == "" and key in os.environ and not os.environ[key]:
                findings.append(
                    Finding(
                        "mcp",
                        "high",
                        "C3.mcp-empty-secret",
                        f"MCP {spec.name} 的 {key} 为空",
                        "server 能启动但首个需要鉴权的调用会 401",
                        str(config),
                        line,
                        fix=f"在启动 dsh 的环境里 export {key}=...",
                    )
                )
            elif effective == "":
                target = env_refs[0] if env_refs else key
                findings.append(
                    Finding(
                        "mcp",
                        "medium",
                        "C3.mcp-env-unset",
                        f"MCP {spec.name} 的 {target} 未设置",
                        "配置里声明了该变量但值为空，相关工具不可用",
                        str(config),
                        line,
                        fix=f"在启动 dsh 的环境里 export {target}=...，或移除无用的 MCP server",
                    )
                )
    return findings


# -- 4. 脚本层 --------------------------------------------------------------
def _check_scripts(root: pathlib.Path, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    patterns = ["testcases/*.py", ".agents/skills/*/scripts/*.py", "testcase-generator-workspace/*.py", "evals/*.py"]
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for snippet, label in _DANGEROUS_SNIPPETS:
                if snippet in text.lower():
                    findings.append(
                        Finding(
                            "safety",
                            "low",
                            "C7.dangerous-snippet",
                            f"脚本内出现危险操作样例：{label}",
                            snippet,
                            str(path),
                            fix="改为参数化路径 + 显式确认，禁止在脚本里硬编码不可逆操作",
                        )
                    )
    return findings


# -- 5. 技能预算 ------------------------------------------------------------
def _check_skill_budgets(root: pathlib.Path, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    playbook = root / ".agents" / "skills" / "testcase-generator" / "references" / "multi-agent-playbook.md"
    if playbook.exists():
        text = playbook.read_text(encoding="utf-8")
        match = re.search(r"总 agent 数控制在\s*(\d+)\s*个以内", text)
        if match:
            documented = int(match.group(1))
            if documented != policy.budget.max_agents:
                findings.append(
                    Finding(
                        "cost",
                        "low",
                        "C6.budget-drift",
                        "编排手册与策略文件的 agent 预算不一致",
                        f"手册写 {documented}，policy 写 {policy.budget.max_agents}",
                        str(playbook),
                        fix="以 governance/policy.yaml 为准，同步手册数字",
                    )
                )
        if "补跑" not in text:
            findings.append(
                Finding(
                    "state",
                    "medium",
                    "C4.no-retry-policy",
                    "编排手册未定义失败分片的补跑规则",
                    "agent() 失败返回 null，静默过滤等于漏测",
                    str(playbook),
                    fix="补上：过滤 null 后对缺失分片补跑一次，仍失败则记入待确认",
                )
            )
    return findings


def format_findings(findings: list[Finding], root: pathlib.Path | None = None) -> str:
    if not findings:
        return "未发现配置级问题。"
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.category, []).append(finding)
    lines: list[str] = []
    for category in CATEGORIES:
        items = grouped.pop(category, [])
        if not items:
            continue
        lines.append(f"\n== {CATEGORIES[category]} ({len(items)}) ==")
        lines.extend(item.format(root) for item in items)
    for category, items in grouped.items():
        lines.append(f"\n== {category} ({len(items)}) ==")
        lines.extend(item.format(root) for item in items)
    summary = {severity: sum(1 for item in findings if item.severity == severity) for severity in _SEVERITY_ORDER}
    lines.append(
        f"\n汇总: high={summary['high']} medium={summary['medium']} low={summary['low']} info={summary['info']}"
    )
    return "\n".join(lines)


def findings_to_json(findings: list[Finding]) -> str:
    return json.dumps([item.to_dict() for item in findings], ensure_ascii=False, indent=2)
