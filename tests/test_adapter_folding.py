"""Tests for adapter-folding in poll_pending_jobs.

Validates that poll_pending_jobs branches on entry.executor to poll
Docker entries via poll_tool() and engine entries via adapter.poll().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bioledger.config import BioLedgerConfig
from bioledger.forges.analysisforge.agent import AnalysisForgeAgent
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.ledger.store import LedgerStore


def _make_running_entry(
    executor: str = "docker",
    run_handle_id: str = "container-abc",
) -> LedgerEntry:
    return LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="samtools_sort",
        container=ContainerInfo(
            image="test:latest",
            command=["test"],
            input_mapping={},
        ),
        files=[FileRef(path="/tmp/out.bam", sha256="abc", size_bytes=100, role="output")],
        run_status="running",
        run_handle_id=run_handle_id,
        executor=executor,
    )


class TestPollPendingJobsAdapterFolding:
    """Test that poll_pending_jobs routes to the correct adapter."""

    def test_docker_entry_uses_poll_tool(self):
        """Docker executor entries should use poll_tool() path."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(executor="docker")
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        with patch(
            "bioledger.forges.analysisforge.agent.poll_tool"
        ) as mock_poll:
            mock_poll.side_effect = lambda e, sd: setattr(
                e, "run_status", "completed"
            ) or setattr(e, "exit_code", 0)
            result = agent.poll_pending_jobs()

        assert len(result) == 1
        assert result[0].run_status == "completed"
        mock_poll.assert_called_once()

    def test_conda_entry_uses_poll_tool(self):
        """Conda executor entries should also use poll_tool() path."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(executor="conda")
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        with patch(
            "bioledger.forges.analysisforge.agent.poll_tool"
        ) as mock_poll:
            mock_poll.side_effect = lambda e, sd: setattr(
                e, "run_status", "completed"
            ) or setattr(e, "exit_code", 0)
            result = agent.poll_pending_jobs()

        assert len(result) == 1
        assert result[0].run_status == "completed"
        mock_poll.assert_called_once()

    def test_snakemake_entry_uses_adapter_poll(self):
        """Snakemake executor entries should use adapter.poll() path."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(
            executor="snakemake", run_handle_id="99999999"
        )
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        from bioledger.core.engines.protocol import RunStatus

        mock_adapter = MagicMock()
        mock_adapter.poll.return_value = RunStatus(
            state="completed", completed=5, total=5
        )

        with patch(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            return_value=mock_adapter,
        ):
            result = agent.poll_pending_jobs()

        assert len(result) == 1
        assert result[0].run_status == "completed"
        assert result[0].exit_code == 0
        mock_adapter.poll.assert_called_once()

    def test_snakemake_entry_failed_status(self):
        """Snakemake entries that fail should get run_status='failed'."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(
            executor="snakemake", run_handle_id="99999999"
        )
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        from bioledger.core.engines.protocol import RunStatus

        mock_adapter = MagicMock()
        mock_adapter.poll.return_value = RunStatus(
            state="failed", message="2/5 jobs failed"
        )

        with patch(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            return_value=mock_adapter,
        ):
            result = agent.poll_pending_jobs()

        assert len(result) == 1
        assert result[0].run_status == "failed"
        assert result[0].exit_code == 1
        assert "2/5 jobs failed" in result[0].notes

    def test_non_running_entries_skipped(self):
        """Entries with run_status != 'running' should be skipped."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(executor="docker")
        entry.run_status = "completed"
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        with patch(
            "bioledger.forges.analysisforge.agent.poll_tool"
        ) as mock_poll:
            result = agent.poll_pending_jobs()

        assert len(result) == 0
        mock_poll.assert_not_called()

    def test_unimplemented_engine_logs_warning(self):
        """Entries with unimplemented engine should log warning, not crash."""
        session = LedgerSession(name="test")
        entry = _make_running_entry(
            executor="nextflow", run_handle_id="nf-run-1"
        )
        session.add(entry)

        config = BioLedgerConfig()
        store = MagicMock(spec=LedgerStore)

        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.session = session
        agent.store = store

        with patch(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            side_effect=NotImplementedError("nextflow adapter not yet"),
        ):
            result = agent.poll_pending_jobs()

        # Entry should remain running (not finalized)
        assert len(result) == 0
        assert entry.run_status == "running"
