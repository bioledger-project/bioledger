"""Tests for the Galaxy adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bioledger.core.engines.config import GalaxyEngineConfig
from bioledger.core.engines.dispatch import _adapter_registry, dispatch
from bioledger.core.engines.galaxy_adapter import GalaxyAdapter
from bioledger.core.engines.protocol import RunHandle


class _MockInput:
    def __init__(self, name="", collection=False, format="any"):
        self.name = name
        self.collection = collection
        self.format = format


class _MockOutput:
    def __init__(self, name="", pattern="*.out", format="any"):
        self.name = name
        self.pattern = pattern
        self.format = format


class _MockSpec:
    def __init__(
        self,
        name="samtools_sort",
        container="quay.io/biocontainers/samtools:1.17",
        conda_deps=None,
        command="samtools sort",
        inputs=None,
        outputs=None,
        executors=None,
    ):
        self.name = name
        self.container = container
        self.conda_deps = conda_deps or []
        self.command = command
        self.inputs = inputs or {"bam": _MockInput("bam", collection=True)}
        self.outputs = outputs or {
            "sorted_bam": _MockOutput("sorted_bam", "*.sorted.bam")
        }
        self.executors = executors or ["galaxy"]


class TestGalaxyGenerate:
    """Galaxy fan-out is deferred — generate() raises NotImplementedError."""

    def test_generate_raises_deferred(self):
        """GalaxyAdapter.generate() raises NotImplementedError for fan-out."""
        import pytest

        adapter = GalaxyAdapter()
        spec = _MockSpec()
        with pytest.raises(NotImplementedError, match="Galaxy fan-out"):
            adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})


class TestGalaxyToolAvailability:
    def test_planemo_mode_skips_check(self):
        """In planemo mode, tool availability check is a no-op (logs only)."""
        config = GalaxyEngineConfig(mode="planemo")
        adapter = GalaxyAdapter(config)
        # Should not raise
        adapter._check_tool_availability("samtools_sort")

    def test_remote_mode_checks_tool_panel(self):
        """In remote mode, tool availability is checked via bioblend."""
        config = GalaxyEngineConfig(
            mode="remote", url="http://galaxy.example", api_key=""
        )
        adapter = GalaxyAdapter(config)
        mock_client = MagicMock()
        mock_client.tools.get_tools.return_value = [
            {"id": "samtools_sort"},
            {"id": "bwa_mem"},
        ]
        adapter._bioblend_client = mock_client
        # Should not raise — tool is available
        adapter._check_tool_availability("samtools_sort")

    def test_remote_mode_raises_for_missing_tool(self):
        """In remote mode, missing tool raises RuntimeError — no Docker fallback."""
        config = GalaxyEngineConfig(
            mode="remote", url="http://galaxy.example", api_key=""
        )
        adapter = GalaxyAdapter(config)
        mock_client = MagicMock()
        mock_client.tools.get_tools.return_value = [
            {"id": "bwa_mem"},
        ]
        adapter._bioblend_client = mock_client
        try:
            adapter._check_tool_availability("nonexistent_tool")
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "nonexistent_tool" in str(e)
            assert "not available" in str(e)
            assert "No Docker fallback" in str(e)


class TestGalaxyPoll:
    def test_poll_planemo_invocation_completed(self):
        """Planemo mode now uses bioblend to poll invocation state."""
        adapter = GalaxyAdapter()  # default mode=planemo
        mock_gi = MagicMock()
        mock_gi.workflows.show_invocation.return_value = {"state": "done"}
        with patch.object(adapter, "_get_galaxy_instance", return_value=mock_gi):
            handle = RunHandle(
                executor="galaxy",
                run_id="inv-123",
                entry_id="",
                work_dir=Path("/tmp"),
            )
            result = adapter.poll(handle)
        assert result is not None
        assert result.state == "completed"
        mock_gi.workflows.show_invocation.assert_called_once_with("inv-123")

    def test_poll_planemo_invocation_failed(self):
        """Planemo mode polls invocation state via bioblend."""
        adapter = GalaxyAdapter()
        mock_gi = MagicMock()
        mock_gi.workflows.show_invocation.return_value = {"state": "failed"}
        with patch.object(adapter, "_get_galaxy_instance", return_value=mock_gi):
            handle = RunHandle(
                executor="galaxy",
                run_id="inv-456",
                entry_id="",
                work_dir=Path("/tmp"),
            )
            result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"

    def test_poll_planemo_invocation_running(self):
        """Planemo mode polls invocation state via bioblend."""
        adapter = GalaxyAdapter()
        mock_gi = MagicMock()
        mock_gi.workflows.show_invocation.return_value = {"state": "running"}
        with patch.object(adapter, "_get_galaxy_instance", return_value=mock_gi):
            handle = RunHandle(
                executor="galaxy",
                run_id="inv-789",
                entry_id="",
                work_dir=Path("/tmp"),
            )
            result = adapter.poll(handle)
        assert result is not None
        assert result.state == "running"

    def test_poll_remote_completed(self):
        config = GalaxyEngineConfig(
            mode="remote", url="http://galaxy.example", api_key=""
        )
        adapter = GalaxyAdapter(config)
        mock_client = MagicMock()
        mock_client.workflows.show_invocation.return_value = {"state": "done"}
        adapter._bioblend_client = mock_client
        handle = RunHandle(
            executor="galaxy",
            run_id="invocation-123",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "completed"

    def test_poll_remote_failed(self):
        config = GalaxyEngineConfig(
            mode="remote", url="http://galaxy.example", api_key=""
        )
        adapter = GalaxyAdapter(config)
        mock_client = MagicMock()
        mock_client.workflows.show_invocation.return_value = {"state": "failed"}
        adapter._bioblend_client = mock_client
        handle = RunHandle(
            executor="galaxy",
            run_id="invocation-123",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"


class TestGalaxyCollect:
    def test_collect_planemo_via_bioblend(self):
        """Planemo mode now collects outputs via bioblend."""
        adapter = GalaxyAdapter()  # default mode=planemo
        mock_gi = MagicMock()
        mock_gi.workflows.show_invocation.return_value = {
            "history_id": "hist-123"
        }
        mock_gi.histories.show_history.return_value = [
            {"state": "ok", "file_name": "/tmp/out1.bam", "name": "out1", "file_size": 100},
            {"state": "error", "file_name": "/tmp/out2.bam", "name": "out2", "file_size": 50},
        ]
        with (
            patch.object(adapter, "_get_galaxy_instance", return_value=mock_gi),
            patch.object(adapter, "_shutdown_planemo") as mock_shutdown,
        ):
            handle = RunHandle(
                executor="galaxy",
                run_id="inv-123",
                entry_id="",
                work_dir=Path("/tmp"),
            )
            outputs = adapter.collect(handle)
        assert len(outputs) == 1
        assert outputs[0]["name"] == "out1"
        mock_shutdown.assert_called_once_with(handle)

    def test_collect_planemo_empty(self):
        """Planemo mode with no outputs returns empty list."""
        adapter = GalaxyAdapter()
        mock_gi = MagicMock()
        mock_gi.workflows.show_invocation.return_value = {
            "history_id": "hist-456"
        }
        mock_gi.histories.show_history.return_value = []
        with (
            patch.object(adapter, "_get_galaxy_instance", return_value=mock_gi),
            patch.object(adapter, "_shutdown_planemo"),
        ):
            handle = RunHandle(
                executor="galaxy",
                run_id="inv-456",
                entry_id="",
                work_dir=Path("/tmp"),
            )
            outputs = adapter.collect(handle)
        assert outputs == []

    def test_collect_remote_via_bioblend(self):
        config = GalaxyEngineConfig(
            mode="remote", url="http://galaxy.example", api_key=""
        )
        adapter = GalaxyAdapter(config)
        mock_client = MagicMock()
        mock_client.workflows.show_invocation.return_value = {
            "history_id": "hist-123"
        }
        mock_client.histories.show_history.return_value = [
            {"state": "ok", "file_name": "/tmp/out1.bam", "name": "out1", "file_size": 100},
            {"state": "error", "file_name": "/tmp/out2.bam", "name": "out2", "file_size": 50},
        ]
        adapter._bioblend_client = mock_client
        handle = RunHandle(
            executor="galaxy",
            run_id="inv-123",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        outputs = adapter.collect(handle)
        # Only state=ok datasets should be collected
        assert len(outputs) == 1
        assert outputs[0]["name"] == "out1"


class TestGalaxyDispatch:
    def test_galaxy_registered_in_registry(self):
        assert "galaxy" in _adapter_registry

    def test_dispatch_returns_galaxy_adapter(self):
        spec = _MockSpec(executors=["galaxy"])
        adapter = dispatch(spec)
        assert isinstance(adapter, GalaxyAdapter)

    def test_dispatch_engine_override_galaxy(self):
        spec = _MockSpec(executors=["docker"])
        adapter = dispatch(spec, engine="galaxy")
        assert isinstance(adapter, GalaxyAdapter)


class TestGalaxySubmitSpec:
    def test_submit_spec_delegates_to_generate_and_submit(self, tmp_path):
        """submit_spec() with tool_dir should call generate() then submit()."""
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        mock_handle = MagicMock()
        mock_run_handle = RunHandle(
            executor="galaxy",
            run_id="inv-123",
            entry_id="",
            work_dir=tmp_path,
        )
        with (
            patch.object(adapter, "generate", return_value=mock_handle) as mock_gen,
            patch.object(adapter, "submit", return_value=mock_run_handle) as mock_sub,
        ):
            result = adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={"threads": "4"},
                work_dir=tmp_path,
                tool_dir=tmp_path / "tool",
            )
        mock_gen.assert_called_once_with(spec, {"input": "/data/file.bam"})
        mock_sub.assert_called_once_with(
            mock_handle, {"input": "/data/file.bam"}, {"threads": "4"}, tmp_path
        )
        assert result == mock_run_handle

    def test_submit_spec_passes_tool_dir_to_metadata(self, tmp_path):
        """submit_spec() with tool_dir should set it in handle metadata."""
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        mock_handle = MagicMock()
        mock_handle.metadata = {}
        mock_run_handle = RunHandle(
            executor="galaxy",
            run_id="inv-456",
            entry_id="",
            work_dir=tmp_path,
        )
        tool_dir = tmp_path / "downloaded_tool"
        tool_dir.mkdir()
        with (
            patch.object(adapter, "generate", return_value=mock_handle),
            patch.object(adapter, "submit", return_value=mock_run_handle),
        ):
            adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={"threads": "4"},
                work_dir=tmp_path,
                tool_dir=tool_dir,
            )
        assert mock_handle.metadata["tool_dir"] == str(tool_dir)
