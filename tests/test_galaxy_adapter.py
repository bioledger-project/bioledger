"""Tests for the Galaxy adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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
    def test_generate_produces_ga_workflow(self, tmp_path):
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        content = handle.artifact_path.read_text()
        data = json.loads(content)
        assert data["a_galaxy_workflow"] == "true"
        assert "steps" in data
        assert handle.executor == "galaxy"
        assert "samtools_sort" in handle.step_inventory

    def test_generate_has_tool_step(self, tmp_path):
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        data = json.loads(handle.artifact_path.read_text())
        # Step 1 should be the tool step
        tool_step = data["steps"]["1"]
        assert tool_step["type"] == "tool"
        assert tool_step["tool_id"] == "samtools_sort"

    def test_generate_has_data_input_step(self, tmp_path):
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        data = json.loads(handle.artifact_path.read_text())
        # Step 0 should be data input
        input_step = data["steps"]["0"]
        assert input_step["type"] == "data_input"

    def test_generate_collection_input_adds_collection_step(self, tmp_path):
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        data = json.loads(handle.artifact_path.read_text())
        # Step 2 should be data collection input
        assert "2" in data["steps"]
        coll_step = data["steps"]["2"]
        assert coll_step["type"] == "data_collection_input"

    def test_generate_stores_collection_inputs(self, tmp_path):
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        assert "collection_inputs" in handle.metadata
        assert "bam" in handle.metadata["collection_inputs"]


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
    def test_poll_planemo_invalid_pid(self):
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id="not-a-pid",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        assert adapter.poll(handle) is None

    def test_poll_planemo_completed(self, tmp_path):
        adapter = GalaxyAdapter()
        log_path = tmp_path / "galaxy.log"
        log_path.write_text("Serving on 0.0.0.0\nShutting down\n")
        handle = RunHandle(
            executor="galaxy",
            run_id="999999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "completed"

    def test_poll_planemo_failed(self, tmp_path):
        adapter = GalaxyAdapter()
        log_path = tmp_path / "galaxy.log"
        log_path.write_text("Error: something went wrong\n")
        handle = RunHandle(
            executor="galaxy",
            run_id="999999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"

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
    def test_collect_planemo_from_galaxy_dir(self, tmp_path):
        adapter = GalaxyAdapter()
        galaxy_dir = tmp_path / "galaxy"
        galaxy_dir.mkdir()
        (galaxy_dir / "output1.dat").write_text("result1")
        (galaxy_dir / "output2.dat").write_text("result2")
        handle = RunHandle(
            executor="galaxy",
            run_id="123",
            entry_id="",
            work_dir=tmp_path,
        )
        outputs = adapter.collect(handle)
        assert len(outputs) == 2
        names = {o["name"] for o in outputs}
        assert "output1.dat" in names
        assert "output2.dat" in names

    def test_collect_planemo_empty(self, tmp_path):
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id="123",
            entry_id="",
            work_dir=tmp_path,
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
