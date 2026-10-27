"""策略加载：把 policy.yaml 变成带类型、带默认值、带校验的对象。

设计要点：
- 缺字段用默认值，不因为策略文件少写一行就整个失效；
- 写错字段（负数、越界）直接报 PolicyError，不静默吞掉；
- 不依赖 PyYAML 也能工作（缺库时退回内置默认值），保证护栏本身不会成为新的故障点。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "PolicyError",
    "LoopPolicy",
    "RetryPolicy",
    "BreakerPolicy",
    "BudgetPolicy",
    "ContextPolicy",
    "SafetyPolicy",
    "McpPolicy",
    "AuditPolicy",
    "Policy",
    "load_policy",
    "default_policy",
]

DEFAULT_POLICY_PATH = pathlib.Path(__file__).with_name("policy.yaml")
POLICY_ENV = "DSH_GOVERNANCE_POLICY"


class PolicyError(ValueError):
    """策略文件非法。宁可启动失败，也不要带着错误阈值跑。"""


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise PolicyError(f"无法解析为布尔值: {value!r}")


def _coerce_int(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"无法解析为整数: {value!r}") from exc
    if isinstance(value, bool):
        raise PolicyError(f"布尔值不能作为整数: {value!r}")
    if minimum is not None and result < minimum:
        raise PolicyError(f"取值 {result} 小于下限 {minimum}")
    if maximum is not None and result > maximum:
        raise PolicyError(f"取值 {result} 超过上限 {maximum}")
    return result


def _coerce_float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"无法解析为数值: {value!r}") from exc
    if minimum is not None and result < minimum:
        raise PolicyError(f"取值 {result} 小于下限 {minimum}")
    if maximum is not None and result > maximum:
        raise PolicyError(f"取值 {result} 超过上限 {maximum}")
    return result


def _coerce_str(value: Any) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"期望字符串，得到 {type(value).__name__}: {value!r}")
    return value


def _coerce_str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"期望字符串列表，得到 {type(value).__name__}: {value!r}")
    return tuple(_coerce_str(item) for item in value)


def _coerce_int_list(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"期望整数列表，得到 {type(value).__name__}: {value!r}")
    return tuple(_coerce_int(item) for item in value)


def _fill(cls: type, mapping: Mapping[str, Any] | None, path: str = "") -> Any:
    """按 dataclass 字段填充，未知字段报错（防止拼错阈值名却以为生效）。"""
    instance = cls()
    mapping = mapping or {}
    if not isinstance(mapping, Mapping):
        raise PolicyError(f"{path or cls.__name__} 必须是映射，得到 {type(mapping).__name__}")
    known = {item.name: item for item in dataclasses.fields(cls)}
    for key, value in mapping.items():
        if key not in known:
            raise PolicyError(f"未知策略字段: {path + '.' if path else ''}{key}")
        current = getattr(instance, key)
        if dataclasses.is_dataclass(current):
            setattr(instance, key, _fill(type(current), value, f"{path + '.' if path else ''}{key}"))
        elif isinstance(current, tuple):
            if current and isinstance(current[0], int):
                setattr(instance, key, _coerce_int_list(value))
            else:
                setattr(instance, key, _coerce_str_list(value))
        elif isinstance(current, bool):
            setattr(instance, key, _coerce_bool(value))
        elif isinstance(current, int):
            setattr(instance, key, _coerce_int(value, minimum=0))
        elif isinstance(current, float):
            setattr(instance, key, _coerce_float(value, minimum=0.0))
        else:
            setattr(instance, key, _coerce_str(value))
    return instance


@dataclass
class LoopPolicy:
    max_rounds: int = 30
    max_tool_calls: int = 120
    deadline_seconds: float = 1800.0
    no_progress_rounds: int = 3
    max_consecutive_failures: int = 2
    duplicate_call_threshold: int = 3

    def validate(self) -> None:
        if self.max_rounds < 1:
            raise PolicyError("loop.max_rounds 至少为 1")
        if self.duplicate_call_threshold < 2:
            raise PolicyError("loop.duplicate_call_threshold 至少为 2，否则第一次调用就会被熔断")
        if self.max_consecutive_failures < 1:
            raise PolicyError("loop.max_consecutive_failures 至少为 1")


@dataclass
class RetryPolicy:
    max_attempts: int = 2
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise PolicyError("retry.max_attempts 至少为 1")
        if self.jitter_ratio > 1.0:
            raise PolicyError("retry.jitter_ratio 不能超过 1.0")


@dataclass
class BreakerPolicy:
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0


@dataclass
class ToolRetryPolicy:
    tool: RetryPolicy = field(default_factory=RetryPolicy)
    llm: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=4))
    breaker: BreakerPolicy = field(default_factory=BreakerPolicy)


@dataclass
class BudgetPolicy:
    max_agents: int = 12
    max_parallel_agents: int = 8
    max_tokens: int = 200000
    max_cost_usd: float = 2.0


@dataclass
class ContextPolicy:
    max_tokens: int = 120000
    keep_recent_turns: int = 6
    tool_result_max_chars: int = 8192
    tool_result_head_chars: int = 4096
    tool_result_tail_chars: int = 1024
    evidence_reserve_tokens: int = 20000

    def validate(self) -> None:
        if self.tool_result_head_chars + self.tool_result_tail_chars > self.tool_result_max_chars:
            raise PolicyError("context 的 head+tail 不能超过 tool_result_max_chars")
        if self.evidence_reserve_tokens >= self.max_tokens:
            raise PolicyError("context.evidence_reserve_tokens 必须小于 max_tokens")


@dataclass
class SafetyPolicy:
    mode: str = "workspace-write"
    require_approval: tuple[str, ...] = ("write", "delete", "network", "config")
    allow_write_roots: tuple[str, ...] = (".",)
    max_write_bytes: int = 2_000_000
    deny_path_parts: tuple[str, ...] = (".git", "node_modules")
    deny_command_patterns: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise PolicyError(f"未知 safety.mode: {self.mode}")


@dataclass
class McpPolicy:
    startup_timeout_seconds: float = 20.0
    health_check_attempts: int = 2
    backoff_seconds: float = 0.5
    required_env: tuple[str, ...] = ()


@dataclass
class AuditPolicy:
    fail_on: str = "high"
    ignore_paths: tuple[str, ...] = (".git", "node_modules", "output", "tmp", "__pycache__")

    def validate(self) -> None:
        if self.fail_on not in {"none", "low", "medium", "high"}:
            raise PolicyError(f"未知 audit.fail_on: {self.fail_on}")


@dataclass
class Policy:
    version: int = 1
    loop: LoopPolicy = field(default_factory=LoopPolicy)
    retry: ToolRetryPolicy = field(default_factory=ToolRetryPolicy)
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    context: ContextPolicy = field(default_factory=ContextPolicy)
    safety: SafetyPolicy = field(default_factory=SafetyPolicy)
    mcp: McpPolicy = field(default_factory=McpPolicy)
    audit: AuditPolicy = field(default_factory=AuditPolicy)

    def validate(self) -> "Policy":
        self.loop.validate()
        self.retry.tool.validate()
        self.retry.llm.validate()
        self.context.validate()
        self.safety.validate()
        self.audit.validate()
        if self.loop.max_tool_calls > self.budget.max_tokens:
            raise PolicyError("loop.max_tool_calls 不应大于 budget.max_tokens（疑似单位写错）")
        return self

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def default_policy() -> Policy:
    return Policy()


def _read_mapping(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} 不是合法 JSON: {exc}") from exc
    else:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise PolicyError(
                f"无法解析 {path}：缺少 PyYAML。可安装依赖，或改用 JSON 策略文件。"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            raise PolicyError(f"{path} 不是合法 YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PolicyError(f"{path} 顶层必须是映射")
    return data


def load_policy(
    path: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    strict_file: bool = True,
) -> Policy:
    """加载策略。

    path 为空时依次尝试：环境变量 DSH_GOVERNANCE_POLICY → 包内 policy.yaml → 内置默认值。
    strict_file=True 时，显式指定的文件不存在会报错；默认文件缺失则退回默认值。
    """
    explicit = path is not None
    if path is None:
        env_path = os.environ.get(POLICY_ENV)
        if env_path:
            path = env_path
        else:
            path = DEFAULT_POLICY_PATH

    target = pathlib.Path(path) if path is not None else None
    mapping: dict[str, Any] = {}
    if target is not None and target.exists():
        mapping = _read_mapping(target)
    elif explicit and strict_file:
        raise PolicyError(f"策略文件不存在: {target}")
    elif os.environ.get(POLICY_ENV) and target is not None and not target.exists():
        raise PolicyError(f"{POLICY_ENV} 指向的策略文件不存在: {target}")

    if overrides:
        mapping = _deep_merge(mapping, dict(overrides))

    policy = Policy(
        version=_coerce_int(mapping.get("version", 1), minimum=1),
        loop=_fill(LoopPolicy, mapping.get("loop"), "loop"),
        retry=_fill(ToolRetryPolicy, mapping.get("retry"), "retry"),
        budget=_fill(BudgetPolicy, mapping.get("budget"), "budget"),
        context=_fill(ContextPolicy, mapping.get("context"), "context"),
        safety=_fill(SafetyPolicy, mapping.get("safety"), "safety"),
        mcp=_fill(McpPolicy, mapping.get("mcp"), "mcp"),
        audit=_fill(AuditPolicy, mapping.get("audit"), "audit"),
    )
    return policy.validate()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
