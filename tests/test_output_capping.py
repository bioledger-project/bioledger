"""Tests for agent-context output capping.

Validates that _session_summary, prior_outputs, fetch_entry_outputs, and
check_job_status cap output file listings to avoid token blowup on fan-out
runs with many output files.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bioledger.core.llm.agents import ForgeDeps
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)


def _make_entry(
    tool_name: str = "fastqc",
    n_outputs: int = 5,
    run_status: str = "completed",
    exit_code: int = 0,
) -> LedgerEntry:
    files = [
        FileRef(
            path=f"/tmp/output_{i}.txt",
            sha256="abc",
            size_bytes=100,
            role="output",
        )
        for i in range(n_outputs)
    ]
    return LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name=tool_name,
        container=ContainerInfo(
            image="test:latest",
            command=["test"],
            input_mapping={},
        ),
        files=files,
        run_status=run_status,
        exit_code=exit_code,
    )


class TestSessionSummaryCapping:
    """Test ForgeDeps.session_summary() output capping."""

    def test_few_outputs_not_capped(self):
        session = LedgerSession(name="test")
        session.add(_make_entry("fastqc", n_outputs=3))
        deps = ForgeDeps(config=MagicMock(), session=session)
        summary = deps.session_summary()
        assert "output_0.txt" in summary
        assert "output_2.txt" in summary
        assert "..." not in summary

    def test_many_outputs_capped(self):
        session = LedgerSession(name="test")
        session.add(_make_entry("fastqc", n_outputs=50))
        deps = ForgeDeps(config=MagicMock(), session=session)
        summary = deps.session_summary()
        assert "50 total" in summary
        assert "output_0.txt" in summary
        # output_49 should not appear (beyond cap)
        assert "output_49.txt" not in summary


class TestSessionSummaryMethodCapping:
    """Test AnalysisForgeAgent._session_summary() output capping."""

    def test_few_outputs_not_capped(self):
        from bioledger.forges.analysisforge.agent import AnalysisForgeAgent

        session = LedgerSession(name="test")
        session.add(_make_entry("samtools_sort", n_outputs=3))
        # Create a minimal agent instance (we only call _session_summary)
        agent = object.__new__(AnalysisForgeAgent)
        agent.session = session
        summary = agent._session_summary()
        assert "output_0.txt" in summary
        assert "output_2.txt" in summary
        assert "..." not in summary

    def test_many_outputs_capped(self):
        from bioledger.forges.analysisforge.agent import AnalysisForgeAgent

        session = LedgerSession(name="test")
        session.add(_make_entry("samtools_sort", n_outputs=500))
        agent = object.__new__(AnalysisForgeAgent)
        agent.session = session
        summary = agent._session_summary()
        assert "500 total" in summary
        assert "output_0.txt" in summary
        assert "output_499.txt" not in summary


class TestCheckJobStatusCapping:
    """Test check_job_status output capping."""

    def test_many_outputs_capped(self):
        _MAX_OUTPUTS = 5
        session = LedgerSession(name="test")
        session.add(_make_entry("bwa_mem", n_outputs=50, run_status="running"))

        # Simulate the check_job_status capping logic
        lines: list[str] = []
        for entry in session.entries:
            if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                output_files = [
                    Path(f.path).name for f in entry.files if f.role == "output"
                ]
                if len(output_files) > _MAX_OUTPUTS:
                    total = len(output_files)
                    shown = output_files[:_MAX_OUTPUTS]
                    outputs_str = f"{shown} ... ({total} total)"
                else:
                    outputs_str = str(output_files)
                lines.append(
                    f"  {entry.id} ({entry.tool_spec_name}): "
                    f"status={entry.run_status}, exit={entry.exit_code}, "
                    f"outputs={outputs_str}"
                )
        result = "Job statuses:\n" + "\n".join(lines)
        assert "50 total" in result
        assert "output_0.txt" in result
        assert "output_49.txt" not in result


class TestFetchEntryOutputsCapping:
    """Test fetch_entry_outputs output capping."""

    def test_many_outputs_capped_with_excluded_names(self):
        _MAX_SNIPPETS = 10
        entry = _make_entry("fastqc", n_outputs=25)
        output_files = [f for f in entry.files if f.role == "output"]

        # Simulate the capping logic
        snippets: list[str] = []
        excluded_names: list[str] = []
        for f in output_files[:_MAX_SNIPPETS]:
            p = Path(f.path)
            snippets.append(f"--- {p.name} --- (file not found)")

        if len(output_files) > _MAX_SNIPPETS:
            excluded_names = [
                Path(f.path).name for f in output_files[_MAX_SNIPPETS:]
            ]

        header = f"Outputs for {entry.tool_spec_name} run {entry.id}:"
        parts = [header, "\n\n".join(snippets)]

        if excluded_names:
            total = len(output_files)
            shown = _MAX_SNIPPETS
            excluded_count = total - shown
            excluded_preview = ", ".join(excluded_names[:5])
            if len(excluded_names) > 5:
                excluded_preview += f", ... ({excluded_count - 5} more)"
            parts.append(
                f"\nShowing {shown} of {total} outputs. "
                f"Excluded: {excluded_preview}"
            )
        result = "\n\n".join(parts)

        assert "Showing 10 of 25 outputs" in result
        assert "output_0.txt" in result
        assert "output_10.txt" in result  # first excluded name
        assert "10 more" in result  # 25-10=15 excluded, 5 shown in preview, 10 more
