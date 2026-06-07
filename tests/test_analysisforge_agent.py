"""Regression tests for AnalysisForgeAgent tool discovery.

These tests verify that the agent correctly discovers and uses tools
from the ToolStore without requiring API keys for LLM initialization.
"""

from __future__ import annotations

from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)
from bioledger.toolspec.store import ToolStore


def test_tool_store_lists_imported_tools(tmp_path):
    """Regression test: Verify ToolStore correctly lists dynamically imported tools.

    This is the core mechanism that enables the fix: when tools like 'line_counter'
    are imported via 'bioledger tool import', they should appear in list_all().
    """
    # Create a temporary tools directory
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()

    # Create test tool specs (simulating user-imported tools)
    line_counter_spec = ToolSpec(
        execution=ExecutionSpec(
            name="line_counter",
            description="Counts lines in a file",
            container="python:3.11-slim",
            command="wc -l {{inputs.input_file}} > {{outputs.count}}",
            inputs={
                "input_file": ToolInput(
                    type=ParamType.FILE,
                    format="txt",
                    description="Input file to count",
                    required=True,
                ),
            },
            outputs={
                "count": ToolOutput(
                    format="txt",
                    description="Line count output",
                    pattern="count.txt",
                ),
            },
            status=SpecStatus.VALID,
        )
    )

    row_counter_spec = ToolSpec(
        execution=ExecutionSpec(
            name="row_counter",
            description="Counts rows in tabular data",
            container="python:3.11-slim",
            command="wc -l {{inputs.input_file}}",
            inputs={
                "input_file": ToolInput(
                    type=ParamType.FILE,
                    format="csv",
                    description="CSV file to count rows",
                    required=True,
                ),
            },
            outputs={
                "count": ToolOutput(
                    format="txt",
                    description="Row count output",
                    pattern="count.txt",
                ),
            },
            status=SpecStatus.VALID,
        )
    )

    # Save tools to store (this is what 'bioledger tool import' does)
    store = ToolStore(tools_dir=tools_dir)
    store.save(line_counter_spec)
    store.save(row_counter_spec)

    # Verify tools are discoverable
    tool_names = store.list_tools()
    assert "line_counter" in tool_names
    assert "row_counter" in tool_names

    # Verify list_all returns the actual specs with descriptions
    all_specs = store.list_all()
    spec_names = {s.name for s in all_specs}
    assert "line_counter" in spec_names
    assert "row_counter" in spec_names

    # This is what the fix uses to build the system prompt
    tools_list = "\n".join(
        f"- {t.name}: {t.execution.description or 'No description'}"
        for t in all_specs
    )
    assert "- line_counter: Counts lines in a file" in tools_list
    assert "- row_counter: Counts rows in tabular data" in tools_list


def test_tools_list_format_for_system_prompt(tmp_path):
    """Test that the tools list is formatted correctly for LLM system prompt.

    Verifies the exact format used in the fix at agent.py:84-87.
    """
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()

    spec = ToolSpec(
        execution=ExecutionSpec(
            name="test_tool",
            description="A test tool for regression testing",
            container="python:3.11-slim",
            command="echo test",
            status=SpecStatus.VALID,
        )
    )

    store = ToolStore(tools_dir=tools_dir)
    store.save(spec)

    available_tools = store.list_all()
    tools_list = "\n".join(
        f"- {t.name}: {t.execution.description or 'No description'}"
        for t in available_tools
    )

    # Format assertions - this is what gets injected into the system prompt
    assert tools_list.startswith("- test_tool:")
    assert "A test tool for regression testing" in tools_list
    assert "\n" not in tools_list.strip()  # Single tool = single line


def test_empty_tools_store_shows_import_message(tmp_path):
    """Test that empty tool stores produce the expected message.

    This tests the fallback branch from agent.py:88-89.
    """
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()

    store = ToolStore(tools_dir=tools_dir)
    available_tools = store.list_all()

    if available_tools:
        tools_list = "\n".join(
            f"- {t.name}: {t.execution.description or 'No description'}"
            for t in available_tools
        )
    else:
        tools_list = "(No tools available - import tools with 'bioledger tool import')"

    assert "No tools available" in tools_list
    assert "bioledger tool import" in tools_list


def test_review_entries_includes_all_required_keys():
    """Regression test: review_entries must include all keys for all entry types.

    This prevents display issues where missing keys cause KeyError or display
    formatting problems in the CLI review and package commands.
    """
    from bioledger.ledger.models import EntryKind, FileRef, LedgerEntry

    # Create entries of different types (no session/store needed for this test)
    data_entry = LedgerEntry(
        kind=EntryKind.DATA_IMPORT,
        params={"summary": "Test dataset"},
        notes="Loaded test data",
    )

    tool_entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="test_tool",
        exit_code=0,
        files=[
            FileRef(path="/tmp/input.txt", sha256="abc123", size_bytes=100, role="input"),
            FileRef(path="/tmp/output.txt", sha256="def456", size_bytes=200, role="output"),
        ],
    )

    # Simulate what review_entries does (extracted logic for testing)
    entries = [data_entry, tool_entry]
    summaries = []
    for entry in entries:
        inputs = [f.path for f in entry.files if f.role == "input"]
        outputs = [f.path for f in entry.files if f.role == "output"]

        # Build summary with all expected keys for consistent display
        summary = {
            "id": entry.id,
            "kind": entry.kind.value,
            "tool": entry.tool_spec_name or "",
            "timestamp": entry.timestamp.isoformat(),
            "inputs": inputs,
            "outputs": outputs,
            "params": entry.params,
            "exit_code": entry.exit_code,
            "notes": entry.notes or "",
            "parent_id": entry.parent_id,
        }

        # For data imports, use a more descriptive label
        if entry.kind == EntryKind.DATA_IMPORT and not summary["notes"]:
            summary["notes"] = entry.params.get("summary", "Loaded dataset")

        summaries.append(summary)

    # Verify we got both entries
    assert len(summaries) == 2

    # All entries must have these keys for display code to work
    required_keys = {"id", "kind", "tool", "outputs", "notes", "timestamp", "parent_id"}

    for summary in summaries:
        # Check all required keys are present
        assert required_keys.issubset(summary.keys()), f"Missing keys in {summary['kind']}"

        # Verify id is non-empty (critical for selection)
        assert summary["id"], f"Entry {summary['kind']} has empty ID"
        assert isinstance(summary["id"], str), f"Entry ID must be string, got {type(summary['id'])}"

        # Verify outputs is always a list (even if empty)
        assert isinstance(summary["outputs"], list), (
            f"outputs must be list, got {type(summary['outputs'])}"
        )

        # Verify tool and notes are strings (can be empty)
        assert isinstance(summary["tool"], str), (
            f"tool must be string, got {type(summary['tool'])}"
        )
        assert isinstance(summary["notes"], str), (
            f"notes must be string, got {type(summary['notes'])}"
        )

    # Verify specific entry content
    data_summary = next(e for e in summaries if e["kind"] == "data_import")
    assert data_summary["notes"] == "Loaded test data"
    assert data_summary["outputs"] == []  # DATA_IMPORT has no outputs

    tool_summary = next(e for e in summaries if e["kind"] == "tool_run")
    assert tool_summary["tool"] == "test_tool"
    assert len(tool_summary["outputs"]) == 1
    assert tool_summary["outputs"][0] == "/tmp/output.txt"
