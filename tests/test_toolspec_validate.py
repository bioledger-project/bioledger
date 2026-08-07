from __future__ import annotations

from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    ToolInput,
    ToolParameter,
)
from bioledger.toolspec.validate import (
    Severity,
    validate_execution,
    validate_spec,
)


def test_validate_valid_spec(sample_exec_spec):
    result = validate_execution(sample_exec_spec)
    assert result.is_valid


def test_validate_missing_container():
    spec = ExecutionSpec(
        name="bad",
        container="",
        command="echo hello",
    )
    result = validate_execution(spec)
    assert not result.is_valid
    errors = [i for i in result.issues if i.severity == Severity.ERROR]
    assert any("executors" in i.field for i in errors)


def test_validate_missing_name():
    spec = ExecutionSpec(
        name="",
        container="ubuntu:latest",
        command="echo hello",
    )
    result = validate_execution(spec)
    assert not result.is_valid
    assert any("name" in i.field for i in result.issues)


def test_validate_missing_command():
    spec = ExecutionSpec(
        name="test",
        container="ubuntu:latest",
        command="",
    )
    result = validate_execution(spec)
    assert not result.is_valid
    assert any("command" in i.field for i in result.issues)


def test_validate_strict_warns_no_outputs():
    spec = ExecutionSpec(
        name="test",
        container="ubuntu:latest",
        command="echo hello",
        inputs={"in": ToolInput(type=ParamType.FILE, format="txt")},
    )
    result = validate_execution(spec, strict=True)
    warnings = [i for i in result.issues if i.severity == Severity.WARNING]
    assert any("output" in i.field.lower() for i in warnings)


def test_validate_full_spec(sample_tool_spec):
    result = validate_spec(sample_tool_spec)
    assert result.is_valid


def test_validate_integer_param_range():
    spec = ExecutionSpec(
        name="test",
        container="ubuntu:latest",
        command="echo",
        parameters={
            "threads": ToolParameter(
                type=ParamType.INTEGER,
                default=100,
                min=1,
                max=32,
            ),
        },
    )
    result = validate_execution(spec, strict=True)
    issues = [i for i in result.issues if "threads" in i.field]
    assert len(issues) > 0
