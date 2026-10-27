import pytest

from governance.retry import ErrorClass
from governance.toolcall import (
    SchemaViolation,
    ToolArgumentError,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    extract_json,
    normalize_tool_name,
    validate_schema,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["read", "write"]},
        "limit": {"type": "integer", "minimum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "raw",
    [
        '{"path": "a.md"}',
        '```json\n{"path": "a.md"}\n```',
        '```\n{"path": "a.md"}\n```',
        '{ "path": "a.md", }',
        "{'path': 'a.md'}",
        '{"path": "a.md"} 请执行',
        '调用参数：{"path": "a.md"}',
        '"{\\"path\\": \\"a.md\\"}"',
        '{"path": "a.md", "limit": None}',
    ],
)
def test_extract_json_accepts_common_model_outputs(raw):
    assert extract_json(raw)["path"] == "a.md"


def test_extract_json_rejects_truncated_payload():
    with pytest.raises(ToolArgumentError) as excinfo:
        extract_json('{"path": "a.md"')
    assert "无法解析" in str(excinfo.value)
    assert excinfo.value.hint


def test_extract_json_rejects_empty():
    with pytest.raises(ToolArgumentError):
        extract_json("   ")


def test_validate_schema_reports_all_violations():
    violations = validate_schema(SCHEMA, {"mode": "delete", "limit": 0, "extra": 1})
    messages = [str(item) for item in violations]
    assert any("缺少必填字段 'path'" in item for item in messages)
    assert any("必须是" in item and "delete" in item for item in messages)
    assert any("minimum" in item for item in messages)
    assert any("额外字段" in item for item in messages)
    assert len(violations) == 4


def test_validate_schema_accepts_valid_payload():
    assert validate_schema(SCHEMA, {"path": "a.md", "mode": "read", "limit": 3, "tags": ["x"]}) == []


def test_validate_schema_handles_boolean_type_and_integer():
    # JSON Schema 里 true 是 boolean，既不是 integer 也不是 number
    assert len(validate_schema({"type": "integer"}, True)) == 1
    assert len(validate_schema({"type": "number"}, True)) == 1
    assert validate_schema({"type": "number"}, 1.5) == []
    assert validate_schema({"type": "boolean"}, True) == []
    assert validate_schema({"type": "string", "minLength": 2}, "ab") == []


def test_normalize_tool_name_strips_mcp_prefix_and_separators():
    assert normalize_tool_name("mcp__playwright__browser_click") == "browser_click"
    assert normalize_tool_name("Read-File") == "read_file"
    assert normalize_tool_name("files.read") == "files_read"


def test_registry_resolves_variants_and_suggests_unknown():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="read_file", input_schema=SCHEMA, handler=lambda args: args["path"]))
    assert registry.resolve("READ_FILE").name == "read_file"
    assert registry.resolve("mcp__fs__read_file").name == "read_file"

    with pytest.raises(UnknownToolError) as excinfo:
        registry.resolve("read_fil")
    assert "read_file" in excinfo.value.candidates


def test_registry_returns_structured_validation_errors():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="read_file", input_schema=SCHEMA, handler=lambda args: "data"))
    result = registry.call("read_file", '{"mode": "read"}')
    assert result.ok is False
    assert result.error_class is ErrorClass.VALIDATION
    assert any("path" in str(item) for item in result.violations)
    assert "修正参数" in result.to_llm()


def test_registry_unknown_tool_is_not_found():
    registry = ToolRegistry()
    result = registry.call("does_not_exist", "{}")
    assert result.ok is False
    assert result.error_class is ErrorClass.NOT_FOUND
    assert "工具不存在" in result.error


def test_registry_maps_handler_exception_to_error_class():
    def boom(_):
        raise PermissionError("permission denied")

    registry = ToolRegistry().register(ToolSpec(name="write_file", handler=boom))
    result = registry.call("write_file", "{}")
    assert result.ok is False
    assert result.error_class is ErrorClass.PERMISSION


def test_registry_happy_path():
    registry = ToolRegistry().register(ToolSpec(name="read_file", input_schema=SCHEMA, handler=lambda args: args["path"].upper()))
    result = registry.call("read_file", '{"path": "a.md"}')
    assert result.ok is True
    assert result.data == "A.MD"
    assert result.duration_ms >= 0


def test_schema_violation_is_readable():
    violation = SchemaViolation("$.path", "缺少必填字段 'path'")
    assert str(violation) == "$.path: 缺少必填字段 'path'"
