"""Regression tests for executor command rendering.

These tests verify the Jinja2 ``inputs.<name>``, ``outputs.<name>.path``, and
``parameters.<name>`` accessors used by tool command templates render correctly
from the dict-shaped ``ExecutionSpec`` collections.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from bioledger.forges.analysisforge.executor import _render_command
from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession
from bioledger.ledger.store import LedgerStore
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolParameter,
    ToolSpec,
)


def test_render_command_outputs_path_attr():
    """outputs.<name>.path must resolve to ``<output_dir>/<pattern>``.

    Templates like hyphy_fel's ``--output {{ outputs.fel_output.path }}`` rely
    on each declared output exposing a ``.path`` attribute in the Jinja context.
    """
    spec = ToolSpec(
        execution=ExecutionSpec(
            name="test_tool",
            container="ubuntu:latest",
            command='echo "out: {{ outputs.result.path }}"',
            outputs={
                "result": ToolOutput(format="txt", pattern="result.txt"),
            },
            status=SpecStatus.VALID,
        )
    )

    input_mapping: dict[str, str] = {}
    params = {}

    cmd = _render_command(spec, input_mapping, params, "/work")
    assert 'out: /work/result.txt' in cmd


def test_render_command_parameter_defaults():
    """Each declared parameter's default must appear in the Jinja context."""
    spec = ToolSpec(
        execution=ExecutionSpec(
            name="test_tool",
            container="ubuntu:latest",
            command='echo "p1: {{ parameters.param1 }}, p2: {{ parameters.param2 }}"',
            parameters={
                "param1": ToolParameter(type=ParamType.STRING, default="default1"),
                "param2": ToolParameter(type=ParamType.INTEGER, default=42),
            },
            status=SpecStatus.VALID,
        )
    )

    input_mapping: dict[str, str] = {}
    params = {}  # Use defaults

    cmd = _render_command(spec, input_mapping, params, "/work")
    assert "p1: default1" in cmd
    assert "p2: 42" in cmd


def test_render_command_input_paths():
    """inputs.<name> must resolve to ``/work/<relative_path>``."""
    spec = ToolSpec(
        execution=ExecutionSpec(
            name="test_tool",
            container="ubuntu:latest",
            command='cat {{ inputs.infile }}',
            inputs={
                "infile": ToolInput(
                    type=ParamType.FILE, format="txt", required=True,
                ),
            },
            status=SpecStatus.VALID,
        )
    )

    input_mapping = {"infile": "file.txt"}
    params = {}

    cmd = _render_command(spec, input_mapping, params, "/work")
    assert "cat /work/file.txt" in cmd


def test_render_command_overrides_defaults():
    """Explicit params dict must override declared defaults."""
    spec = ToolSpec(
        execution=ExecutionSpec(
            name="test_tool",
            container="ubuntu:latest",
            command='echo "val: {{ parameters.threshold }}"',
            parameters={
                "threshold": ToolParameter(type=ParamType.FLOAT, default=0.05),
            },
            status=SpecStatus.VALID,
        )
    )

    input_mapping: dict[str, str] = {}
    params = {"threshold": 0.01}  # Override default

    cmd = _render_command(spec, input_mapping, params, "/work")
    assert "val: 0.01" in cmd


def test_render_command_hyphy_fel_pattern():
    """Reproduces the exact ``outputs.fel_output.path`` access pattern from
    hyphy_fel that previously rendered as ``'dict object' has no attribute
    'fel_output'``.
    """
    spec = ToolSpec(
        execution=ExecutionSpec(
            name="hyphy_fel",
            container="hyphy/hyphy:2.5.31",
            command='--output "{{ outputs.fel_output.path }}"',
            inputs={
                "input_file": ToolInput(
                    type=ParamType.FILE, format="fasta", required=True,
                ),
            },
            outputs={
                "fel_output": ToolOutput(format="json", pattern="fel_output.json"),
            },
            status=SpecStatus.VALID,
        )
    )

    input_mapping = {"input_file": "alignment.fas"}
    params = {}

    cmd = _render_command(spec, input_mapping, params, "/work")
    assert '--output "/work/fel_output.json"' in cmd


def test_entry_id_persists_through_session_save_load():
    """Regression: Entry IDs must persist when session is saved and reloaded.

    Issue: Second tool run's entry ID was None in review output.
    This tests that IDs are preserved through the save/load cycle.
    """
    with TemporaryDirectory() as tmpdir:
        # Create store with temp DB
        store = LedgerStore(db_path=Path(tmpdir) / "test.db")

        # Create session with two tool run entries
        session = LedgerSession(name="test_session")
        store.create_session(session)

        entry1 = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="fastqc",
        )
        entry2 = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="hyphy_fel",
        )

        session.add(entry1)
        session.add(entry2)
        store.save_session(session)

        # Reload session from DB
        loaded_session = store.load_session(session.id)

        # Both entries must have IDs
        assert len(loaded_session.entries) == 2
        assert loaded_session.entries[0].id is not None
        assert loaded_session.entries[1].id is not None
        assert loaded_session.entries[0].id != loaded_session.entries[1].id
        assert loaded_session.entries[0].tool_spec_name == "fastqc"
        assert loaded_session.entries[1].tool_spec_name == "hyphy_fel"


def test_review_entries_includes_all_ids():
    """Regression: review_entries() must include ID for all entries.

    Issue: Second tool run's entry ID was missing in review output.
    This tests that entries have IDs when added to session.
    """
    session = LedgerSession(name="test_session")
    entry1 = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="fastqc",
    )
    entry2 = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="hyphy_fel",
    )
    session.add(entry1)
    session.add(entry2)

    # Both entries must have IDs
    assert len(session.entries) == 2
    assert session.entries[0].id is not None
    assert session.entries[1].id is not None
    assert session.entries[0].id != session.entries[1].id
    assert session.entries[0].tool_spec_name == "fastqc"
    assert session.entries[1].tool_spec_name == "hyphy_fel"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
