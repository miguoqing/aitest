"""工具调用层：容错解析 → 参数校验 → 工具解析 → 结构化错误。

线上最常见的三类工具故障，处理方式完全不同：

- 参数 JSON 坏了（多一层代码块、尾逗号、单引号、截断）——能修就修，修不了
  要把原始片段和修复建议回给模型，而不是抛一个 JSONDecodeError 让循环崩掉。
- 参数结构不对（缺必填、类型错、枚举越界）——把 json-schema 违规点逐条列出来，
  模型下一轮才知道改哪；只说"参数错误"等于让它盲猜。
- 工具名写错（大小写、mcp__server__tool 前缀、翻译过的名字）——先归一化，再用
  相似度给出候选，避免模型反复试一个不存在的工具。
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .retry import ErrorClass, classify_error

__all__ = [
    "ToolArgumentError",
    "SchemaViolation",
    "UnknownToolError",
    "ToolSpec",
    "ToolCallResult",
    "ToolRegistry",
    "extract_json",
    "validate_schema",
    "normalize_tool_name",
]

_MAX_SCHEMA_DEPTH = 32


class ToolArgumentError(ValueError):
    def __init__(self, message: str, *, raw: Any = None, hint: str = "") -> None:
        self.raw = raw
        self.hint = hint
        super().__init__(message)


class UnknownToolError(KeyError):
    def __init__(self, name: str, candidates: list[str]) -> None:
        self.name = name
        self.candidates = candidates
        hint = f"；相近工具: {', '.join(candidates)}" if candidates else ""
        super().__init__(f"工具不存在: {name!r}{hint}")


@dataclass(frozen=True)
class SchemaViolation:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u3000": " "})
_PY_LITERALS = ((re.compile(r"\bTrue\b"), "true"), (re.compile(r"\bFalse\b"), "false"), (re.compile(r"\bNone\b"), "null"))


def extract_json(raw: Any) -> Any:
    """尽最大努力把模型吐出的参数还原成对象；失败时抛可读的 ToolArgumentError。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, list):
        return raw
    if raw is None:
        raise ToolArgumentError("工具参数为空", raw=None, hint="请传一个 JSON 对象，例如 {\"path\": \"wiki/index.md\"}")
    if not isinstance(raw, str):
        raise ToolArgumentError(f"工具参数类型不支持: {type(raw).__name__}", raw=raw, hint="参数应为 JSON 字符串或对象")

    text = raw.strip().lstrip("\ufeff")
    if not text:
        raise ToolArgumentError("工具参数是空字符串", raw=raw, hint="请传一个 JSON 对象")

    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    candidates: list[str] = [text]
    balanced = _first_balanced(text)
    if balanced and balanced != text:
        candidates.append(balanced)

    for candidate in candidates:
        parsed = _try_parse(candidate)
        if parsed is not _FAILED:
            return _unwrap_double_encoded(parsed)
        repaired = _repair(candidate)
        if repaired != candidate:
            parsed = _try_parse(repaired)
            if parsed is not _FAILED:
                return _unwrap_double_encoded(parsed)
        literal = _python_literal(candidate)
        if literal is not _FAILED:
            return _unwrap_double_encoded(literal)

    preview = text[:200] + ("…" if len(text) > 200 else "")
    raise ToolArgumentError(
        "无法解析工具参数 JSON",
        raw=raw,
        hint=f"请检查引号/逗号是否配对，原始片段: {preview}",
    )


_FAILED = object()


def _try_parse(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _FAILED


def _unwrap_double_encoded(value: Any) -> Any:
    # 模型偶尔把对象再序列化一次，得到 '{"a": 1}' 这样的字符串
    for _ in range(2):
        if isinstance(value, str) and value.strip().startswith(("{", "[")):
            parsed = _try_parse(value)
            if parsed is not _FAILED:
                value = parsed
                continue
        break
    return value


def _first_balanced(text: str) -> str | None:
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None  # 截断的 JSON：交给 _repair 报可读错误


def _repair(text: str) -> str:
    repaired = text.translate(_SMART_QUOTES)
    repaired = repaired.replace("\r\n", "\n")
    repaired = _TRAILING_COMMA_RE.sub(r"\1", repaired)
    for pattern, replacement in _PY_LITERALS:
        repaired = pattern.sub(replacement, repaired)
    return repaired


def _python_literal(text: str) -> Any:
    if not text.lstrip().startswith(("{", "[")):
        return _FAILED
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return _FAILED
    if isinstance(value, (dict, list)):
        return value
    return _FAILED


# --------------------------------------------------------------------------
# JSON Schema 子集校验
# --------------------------------------------------------------------------
_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
    "null": type(None),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py_type = _TYPE_MAP.get(expected)
    if py_type is None:
        return True  # 未知类型不阻断，避免 schema 方言差异造成误杀
    return isinstance(value, py_type)


def validate_schema(schema: Any, value: Any, path: str = "$", _depth: int = 0) -> list[SchemaViolation]:
    """校验 json-schema 子集，返回全部违规点（不是只报第一个）。"""
    violations: list[SchemaViolation] = []
    if _depth > _MAX_SCHEMA_DEPTH:
        return [SchemaViolation(path, "schema 嵌套过深，疑似循环引用")]
    if schema is True or schema == {}:
        return violations
    if schema is False:
        return [SchemaViolation(path, "该字段不允许出现")]
    if not isinstance(schema, Mapping):
        return violations

    expected = schema.get("type")
    if isinstance(expected, str):
        if not _type_ok(value, expected):
            violations.append(SchemaViolation(path, f"期望类型 {expected}，实际 {type(value).__name__}"))
            return violations
    elif isinstance(expected, list):
        if not any(_type_ok(value, item) for item in expected if isinstance(item, str)):
            violations.append(SchemaViolation(path, f"期望类型 {expected} 之一，实际 {type(value).__name__}"))
            return violations

    if "const" in schema and value != schema["const"]:
        violations.append(SchemaViolation(path, f"必须等于 {schema['const']!r}"))
    if "enum" in schema and value not in schema["enum"]:
        violations.append(SchemaViolation(path, f"必须是 {schema['enum']} 之一，实际 {value!r}"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            violations.append(SchemaViolation(path, f"长度 {len(value)} 小于 minLength={schema['minLength']}"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            violations.append(SchemaViolation(path, f"长度 {len(value)} 大于 maxLength={schema['maxLength']}"))
        if "pattern" in schema:
            try:
                if re.search(schema["pattern"], value) is None:
                    violations.append(SchemaViolation(path, f"不匹配模式 {schema['pattern']!r}"))
            except re.error:
                pass

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(SchemaViolation(path, f"小于 minimum={schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            violations.append(SchemaViolation(path, f"大于 maximum={schema['maximum']}"))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            violations.append(SchemaViolation(path, f"必须大于 {schema['exclusiveMinimum']}"))

    if isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in value:
                violations.append(SchemaViolation(path, f"缺少必填字段 {required!r}"))
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if isinstance(properties, Mapping) and key in properties:
                violations.extend(validate_schema(properties[key], item, child_path, _depth + 1))
            elif schema.get("additionalProperties") is False:
                violations.append(SchemaViolation(child_path, "不允许的额外字段"))
            elif isinstance(schema.get("additionalProperties"), Mapping):
                violations.extend(validate_schema(schema["additionalProperties"], item, child_path, _depth + 1))
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            violations.append(SchemaViolation(path, f"字段数少于 minProperties={schema['minProperties']}"))

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                violations.extend(validate_schema(items, item, f"{path}[{index}]", _depth + 1))
        elif isinstance(items, list):
            for index, item in enumerate(value):
                if index < len(items):
                    violations.extend(validate_schema(items[index], item, f"{path}[{index}]", _depth + 1))
        if "minItems" in schema and len(value) < schema["minItems"]:
            violations.append(SchemaViolation(path, f"元素数少于 minItems={schema['minItems']}"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            violations.append(SchemaViolation(path, f"元素数多于 maxItems={schema['maxItems']}"))

    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            branch_results = [validate_schema(branch, value, path, _depth + 1) for branch in branches]
            passed = sum(1 for result in branch_results if not result)
            if keyword == "oneOf" and passed != 1:
                violations.append(SchemaViolation(path, f"oneOf 需要恰好 1 个分支匹配，实际 {passed} 个"))
            if keyword == "anyOf" and passed == 0:
                violations.append(SchemaViolation(path, "anyOf 没有任何分支匹配"))

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            violations.extend(validate_schema(branch, value, path, _depth + 1))

    return violations


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------
def normalize_tool_name(name: str) -> str:
    normalized = name.strip().lower()
    normalized = re.sub(r"[\s./:\-]+", "_", normalized)
    normalized = re.sub(r"^mcp__[a-z0-9_]+__", "", normalized)
    normalized = re.sub(r"^mcp_", "", normalized)
    return normalized


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk: str = "read"
    handler: Callable[[dict[str, Any]], Any] | None = None


@dataclass
class ToolCallResult:
    ok: bool
    tool: str
    data: Any = None
    error: str | None = None
    error_class: ErrorClass | None = None
    violations: list[SchemaViolation] = field(default_factory=list)
    attempts: int = 1
    duration_ms: float = 0.0
    raw: Any = None

    def to_llm(self) -> str:
        """给模型看的紧凑结果：成功给数据，失败给可执行的修复建议。"""
        if self.ok:
            return json.dumps(self.data, ensure_ascii=False, default=str)[:4000]
        parts = [f"工具 {self.tool!r} 调用失败（{self.error_class.value if self.error_class else 'unknown'}）: {self.error}"]
        if self.violations:
            parts.append("参数问题：")
            parts.extend(f"- {item}" for item in self.violations[:10])
        parts.append("请修正参数后重试；同一参数不要重复提交。")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "error": self.error,
            "error_class": self.error_class.value if self.error_class else None,
            "violations": [str(item) for item in self.violations],
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._normalized: dict[str, str] = {}

    def register(self, spec: ToolSpec) -> "ToolRegistry":
        if not spec.name:
            raise ValueError("工具必须有名字")
        self._specs[spec.name] = spec
        self._normalized.setdefault(normalize_tool_name(spec.name), spec.name)
        return self

    def __contains__(self, name: object) -> bool:
        try:
            self.resolve(str(name))
        except UnknownToolError:
            return False
        return True

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def resolve(self, name: str) -> ToolSpec:
        if name in self._specs:
            return self._specs[name]
        normalized = normalize_tool_name(name)
        if normalized in self._normalized:
            return self._specs[self._normalized[normalized]]
        suffix = [item for item in self._specs if normalize_tool_name(item).endswith(normalized) and normalized]
        if len(suffix) == 1:
            return self._specs[suffix[0]]
        # 不做自动纠错：把相似工具作为候选交给模型决定，避免"改名后跑错工具"
        candidates = difflib.get_close_matches(name, list(self._specs), n=3, cutoff=0.5)
        raise UnknownToolError(name, candidates)

    def call(self, name: str, raw_arguments: Any = None) -> ToolCallResult:
        started = time.perf_counter()
        try:
            spec = self.resolve(name)
        except UnknownToolError as exc:
            return ToolCallResult(
                ok=False,
                tool=name,
                error=str(exc),
                error_class=ErrorClass.NOT_FOUND,
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )

        try:
            arguments = extract_json(raw_arguments)
        except ToolArgumentError as exc:
            return ToolCallResult(
                ok=False,
                tool=spec.name,
                error=f"{exc}（{exc.hint}）" if exc.hint else str(exc),
                error_class=ErrorClass.VALIDATION,
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )

        if not isinstance(arguments, Mapping):
            return ToolCallResult(
                ok=False,
                tool=spec.name,
                error=f"参数必须是 JSON 对象，实际是 {type(arguments).__name__}",
                error_class=ErrorClass.VALIDATION,
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )

        violations = validate_schema(spec.input_schema, arguments)
        if violations:
            return ToolCallResult(
                ok=False,
                tool=spec.name,
                error=f"参数校验未通过，共 {len(violations)} 处",
                error_class=ErrorClass.VALIDATION,
                violations=violations,
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )

        if spec.handler is None:
            return ToolCallResult(
                ok=True,
                tool=spec.name,
                data={"validated": True, "arguments": dict(arguments)},
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )

        try:
            data = spec.handler(dict(arguments))
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return ToolCallResult(
                ok=False,
                tool=spec.name,
                error=f"{type(exc).__name__}: {exc}",
                error_class=classify_error(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
                raw=raw_arguments,
            )
        return ToolCallResult(
            ok=True,
            tool=spec.name,
            data=data,
            duration_ms=(time.perf_counter() - started) * 1000,
            raw=raw_arguments,
        )
