"""Tests for agent-driven fan-out path (Phase 2b-2).

Validates submit_fanout: resolve engine → generate → create entry → submit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bioledger.config import BioLedgerConfig
from bioledger.core.engines.protocol import RunHandle, RunStatus, WorkflowHandle
from bioledger.forges.analysisforge.agent import AnalysisForgeAgent
from bioledger.ledger.models import EntryKind, LedgerSession
from bioledger.ledger.store import LedgerStore
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)
from bioledger.toolspec.store import ToolStore


def _make_fanout_spec() -> ToolSpec:
    exec_spec = ExecutionSpec(
        name="samtools_sort",
        container="quay.io/biocontainers/samtools:1.17",
        command="samtools sort -o {{outputs.sorted.path}} {{inputs.bam}}",
        inputs={
            "bam": ToolInput(
                type=ParamType.FILE, format="bam", required=True,
                collection=True,
            ),
        },
        outputs={
            "sorted": ToolOutput(format="bam", pattern="*.sorted.bam"),
        },
        status=SpecStatus.VALID,
    )
    return ToolSpec(execution=exec_spec)


class TestSubmitFanout:
    """Test the agent-driven fan-out submission path."""

    def _make_agent(self, tmp_path: Path, spec: ToolSpec) -> AnalysisForgeAgent:
        config = BioLedgerConfig(home_dir=str(tmp_path))
        tool_store = MagicMock(spec=ToolStore)
        tool_store.has.return_value = True
        tool_store.load.return_value = spec
        ledger_store = MagicMock(spec=LedgerStore)
        session = LedgerSession(name="test")
        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.tool_store = tool_store
        agent.store = ledger_store
        agent.session = session
        agent._tool_library = {}
        return agent

    def test_submit_fanout_creates_running_entry(self, tmp_path):
        """submit_fanout should create a LedgerEntry with run_status='running'."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["samtools_sort"],
            container_image="quay.io/biocontainers/samtools:1.17",
            input_mapping={"bam": "data/*.bam"},
            output_patterns=["{sample}.sorted.bam"],
            metadata={"collection_inputs": ["bam"]},
        )
        mock_run_handle = RunHandle(
            executor="snakemake",
            run_id="12345",
            entry_id="",
            work_dir=tmp_path,
        )

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            entry, handle = agent.submit_fanout(
                tool_name="samtools_sort",
                input_mapping={"bam": "data/*.bam"},
                params={},
            )

        assert entry.run_status == "running"
        assert entry.executor == "snakemake"
        assert entry.run_handle_id == "12345"
        assert entry.tool_spec_name == "samtools_sort"
        assert "Fan-out via snakemake" in entry.notes
        assert handle is not None
        assert handle.executor == "snakemake"
        mock_adapter.generate.assert_called_once()
        mock_adapter.submit.assert_called_once()

    def test_submit_fanout_explicit_engine(self, tmp_path):
        """Explicit engine override should be passed through."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["samtools_sort"],
            container_image="quay.io/biocontainers/samtools:1.17",
            input_mapping={"bam": "data/*.bam"},
            output_patterns=["{sample}.sorted.bam"],
        )
        mock_run_handle = RunHandle(
            executor="snakemake",
            run_id="99999",
            entry_id="",
            work_dir=tmp_path,
        )

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            entry, _ = agent.submit_fanout(
                tool_name="samtools_sort",
                input_mapping={"bam": "data/*.bam"},
                params={"threads": "4"},
                engine="snakemake",
            )

        assert entry.executor == "snakemake"
        assert entry.params == {"threads": "4"}

    def test_submit_fanout_no_engine_raises(self, tmp_path):
        """Should raise RuntimeError when no engine is available."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        with patch(
            "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
            side_effect=RuntimeError("No engine available"),
        ):
            try:
                agent.submit_fanout(
                    tool_name="samtools_sort",
                    input_mapping={"bam": "data/*.bam"},
                    params={},
                )
                assert False, "Should have raised RuntimeError"
            except RuntimeError as e:
                assert "No engine" in str(e)

    def test_submit_fanout_saves_session(self, tmp_path):
        """submit_fanout should persist the session after creating the entry."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["samtools_sort"],
            container_image="quay.io/biocontainers/samtools:1.17",
            input_mapping={"bam": "data/*.bam"},
            output_patterns=["{sample}.sorted.bam"],
        )
        mock_run_handle = RunHandle(
            executor="snakemake",
            run_id="77777",
            entry_id="",
            work_dir=tmp_path,
        )

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            agent.submit_fanout(
                tool_name="samtools_sort",
                input_mapping={"bam": "data/*.bam"},
                params={},
            )

        agent.store.save_session.assert_called_once_with(agent.session)

    def test_submit_fanout_entry_has_correct_kind(self, tmp_path):
        """Entry should be TOOL_RUN kind."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["samtools_sort"],
            container_image="quay.io/biocontainers/samtools:1.17",
            input_mapping={"bam": "data/*.bam"},
            output_patterns=["{sample}.sorted.bam"],
        )
        mock_run_handle = RunHandle(
            executor="snakemake",
            run_id="55555",
            entry_id="",
            work_dir=tmp_path,
        )

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            entry, _ = agent.submit_fanout(
                tool_name="samtools_sort",
                input_mapping={"bam": "data/*.bam"},
                params={},
            )

        assert entry.kind == EntryKind.TOOL_RUN

    def test_submit_fanout_poll_integration(self, tmp_path):
        """After submit_fanout, poll_pending_jobs should poll via adapter."""
        agent = self._make_agent(tmp_path, _make_fanout_spec())

        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["samtools_sort"],
            container_image="quay.io/biocontainers/samtools:1.17",
            input_mapping={"bam": "data/*.bam"},
            output_patterns=["{sample}.sorted.bam"],
            metadata={"collection_inputs": ["bam"]},
        )
        mock_run_handle = RunHandle(
            executor="snakemake",
            run_id="33333",
            entry_id="",
            work_dir=tmp_path,
        )

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle
        mock_adapter.poll.return_value = RunStatus(
            state="completed", completed=5, total=5
        )

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            entry, _ = agent.submit_fanout(
                tool_name="samtools_sort",
                input_mapping={"bam": "data/*.bam"},
                params={},
            )

            # Now poll — should use adapter.poll()
            completed = agent.poll_pending_jobs()

        assert len(completed) == 1
        assert completed[0].run_status == "completed"
        assert completed[0].exit_code == 0
