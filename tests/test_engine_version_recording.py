"""Tests for engine version recording at submit time.

Verifies that:
- ``LedgerEntry.executor_version`` is populated from ``RunHandle.metadata``
  after ``submit_fanout`` and ``submit_discovered``.
- ``_detect_version()`` on each adapter caches the result (subprocess.run
  called once).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.protocol import RunHandle, WorkflowHandle
from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession


def _make_run_handle(executor: str, version: str) -> RunHandle:
    """Create a RunHandle with engine_version metadata."""
    return RunHandle(
        executor=executor,
        run_id="test-run-id",
        entry_id="test-entry-id",
        work_dir=Path("/tmp/work"),
        status="running",
        metadata={"engine_version": version},
    )


class TestDetectVersionCaching:
    """Test that _detect_version() caches the result."""

    def test_detect_version_caches_snakemake(self):
        """SnakemakeAdapter._detect_version() should shell out only once."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        with patch("bioledger.core.engines.versions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="7.32.4\n", stderr="")
            with patch("bioledger.core.engines.versions.shutil.which", return_value="/usr/bin/snakemake"):
                v1 = adapter._detect_version()
                v2 = adapter._detect_version()

        assert v1 == v2
        assert "snakemake" in v1
        mock_run.assert_called_once()

    def test_detect_version_caches_nextflow(self):
        """NextflowAdapter._detect_version() should shell out only once."""
        from bioledger.core.engines.nextflow_adapter import NextflowAdapter

        adapter = NextflowAdapter()
        with patch("bioledger.core.engines.versions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Nextflow version 23.10.0 build 5891\n", stderr=""
            )
            with patch("bioledger.core.engines.versions.shutil.which", return_value="/usr/bin/nextflow"):
                v1 = adapter._detect_version()
                v2 = adapter._detect_version()

        assert v1 == v2
        assert "nextflow" in v1
        mock_run.assert_called_once()

    def test_detect_version_caches_galaxy(self):
        """GalaxyAdapter._detect_version() should shell out only once."""
        from bioledger.core.engines.galaxy_adapter import GalaxyAdapter
        from bioledger.core.engines.config import GalaxyEngineConfig

        adapter = GalaxyAdapter(GalaxyEngineConfig(mode="planemo"))
        with patch("bioledger.core.engines.versions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="planemo 0.74.0\n", stderr="")
            with patch("bioledger.core.engines.versions.shutil.which", return_value="/usr/bin/planemo"):
                v1 = adapter._detect_version()
                v2 = adapter._detect_version()

        assert v1 == v2
        assert "galaxy" in v1
        mock_run.assert_called_once()


class TestExecutorVersionPopulated:
    """Test that executor_version is populated from RunHandle.metadata."""

    def test_executor_version_populated_on_fanout(self):
        """submit_fanout should set entry.executor_version from RunHandle.metadata."""
        from bioledger.forges.analysisforge.agent import AnalysisForgeAgent
        from bioledger.config import BioLedgerConfig

        # Build a minimal agent with a mocked session and config
        session = LedgerSession(name="test-fanout")
        config = MagicMock(spec=BioLedgerConfig)
        config.home_dir = Path("/tmp/bioledger-test")
        config.engines = None

        agent = AnalysisForgeAgent.__new__(AnalysisForgeAgent)
        agent.session = session
        agent.config = config
        agent.store = MagicMock()
        agent.store.save_session = MagicMock()

        # Mock the adapter chain: resolve_engine → get_tool_spec → generate → submit
        mock_adapter = MagicMock()
        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path="/tmp/Snakefile",
        )
        mock_adapter.generate.return_value = mock_handle
        mock_run_handle = _make_run_handle("snakemake", "snakemake 7.32.4")
        mock_adapter.submit.return_value = mock_run_handle

        mock_spec = MagicMock()
        mock_spec.execution.model_dump.return_value = {"name": "test_tool"}
        mock_spec.execution.container = ""

        with (
            patch("bioledger.core.engines.dispatch.resolve_engine_for_fanout", return_value="snakemake"),
            patch("bioledger.core.engines.dispatch._get_engine_adapter", return_value=mock_adapter),
            patch.object(agent, "get_tool_spec", return_value=mock_spec),
            patch("bioledger.forges.analysisforge.agent._write_engine_metadata_sidecar"),
            patch("bioledger.forges.analysisforge.agent.get_session_dir", return_value=Path("/tmp/bioledger-test/sessions/test")),
        ):
            entry, _ = agent.submit_fanout(
                tool_name="test_tool",
                input_mapping={"bam": "data/*.bam"},
                params={},
                engine="snakemake",
            )

        assert entry.executor_version == "snakemake 7.32.4"
        assert entry.executor == "snakemake"

    def test_executor_version_populated_on_discovered(self):
        """submit_discovered should set entry.executor_version from RunHandle.metadata.

        Mocks the nf-core download/parse path to avoid network calls.
        """
        from bioledger.forges.analysisforge.agent import AnalysisForgeAgent
        from bioledger.config import BioLedgerConfig

        session = LedgerSession(name="test-discovered")
        config = MagicMock(spec=BioLedgerConfig)
        config.home_dir = Path("/tmp/bioledger-test")
        config.engines = None

        agent = AnalysisForgeAgent.__new__(AnalysisForgeAgent)
        agent.session = session
        agent.config = config
        agent.store = MagicMock()
        agent.store.save_session = MagicMock()

        mock_adapter = MagicMock()
        mock_run_handle = _make_run_handle("nextflow", "nextflow 23.10.0")
        mock_adapter.submit_spec.return_value = mock_run_handle

        with (
            patch("bioledger.core.engines.dispatch._get_engine_adapter", return_value=mock_adapter),
            patch("bioledger.forges.analysisforge.agent._write_engine_metadata_sidecar"),
            patch("bioledger.forges.analysisforge.agent.get_session_dir", return_value=Path("/tmp/bioledger-test/sessions/test")),
            patch("bioledger.core.engines.nfcore_search.download_module", return_value=Path("/tmp/nf-module")),
            patch("bioledger.core.engines.nfcore_search.parse_meta_yml", return_value={
                "inputs": ["reads"],
                "outputs": {"bam": "*.bam"},
                "container": "quay.io/biocontainers/test:1.0",
            }),
        ):
            entry, rh = agent.submit_discovered(
                tool_name="nf_test",
                discovered_source="nfcore:rna_seq",
                input_mapping={"reads": "data/*.fastq"},
                params={},
            )

        assert entry.executor_version == "nextflow 23.10.0"
        assert entry.executor == "nextflow"
