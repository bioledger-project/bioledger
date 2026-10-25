"""Tests for the Galaxy adapter."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
        adapter = GalaxyAdapter()
        spec = _MockSpec()
        with pytest.raises(NotImplementedError, match="Galaxy fan-out"):
            adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})


class TestGalaxySubmit:
    """Fan-out submit() is unreachable — generate() always raises."""

    def test_submit_raises_deferred(self):
        adapter = GalaxyAdapter()
        handle = MagicMock()
        with pytest.raises(NotImplementedError, match="Galaxy fan-out"):
            adapter.submit(
                handle=handle, input_mapping={}, params={}, work_dir=Path("/tmp")
            )


class TestGalaxyPoll:
    """poll() on submit_spec() runs — local planemo subprocesses polled
    via the shared PID + .exit_code path."""

    def test_poll_returns_none_for_non_pid(self):
        """Non-numeric run_id (e.g. a stray invocation ID) is a lost run."""
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id="inv-123",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        assert adapter.poll(handle) is None

    def test_poll_running_process(self, tmp_path):
        """A live PID reports running."""
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id=str(os.getpid()),
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "running"

    def test_poll_completed_via_exit_code(self, tmp_path):
        """A dead PID with .exit_code=0 reports completed."""
        adapter = GalaxyAdapter()
        (tmp_path / ".exit_code").write_text("0")
        handle = RunHandle(
            executor="galaxy",
            run_id="99999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "completed"

    def test_poll_failed_via_exit_code(self, tmp_path):
        """A dead PID with nonzero .exit_code reports failed."""
        adapter = GalaxyAdapter()
        (tmp_path / ".exit_code").write_text("2")
        handle = RunHandle(
            executor="galaxy",
            run_id="99999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"

    def test_poll_failed_log_fallback(self, tmp_path):
        """No .exit_code + error in galaxy.log → failed."""
        adapter = GalaxyAdapter()
        (tmp_path / "galaxy.log").write_text("Error: planemo blew up\n")
        handle = RunHandle(
            executor="galaxy",
            run_id="99999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"

    def test_poll_unknown_without_sidecar_or_log(self, tmp_path):
        """No .exit_code and no log markers → unknown."""
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id="99999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "unknown"


class TestGalaxyCollect:
    def test_collect_returns_empty(self):
        """Local-run outputs are file-discovered — collect() is a no-op."""
        adapter = GalaxyAdapter()
        handle = RunHandle(
            executor="galaxy",
            run_id="12345",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        assert adapter.collect(handle) == []


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
    def test_submit_spec_renders_command_and_returns_runhandle(self, tmp_path):
        """submit_spec() should Jinja-render spec.command and launch as subprocess."""
        adapter = GalaxyAdapter()
        spec = _MockSpec(
            command=(
                "planemo run tool.xml job.yml "
                "--input {{ inputs.input }} "
                "--outdir {{ outputs._dir }}"
            ),
            inputs={"input": _MockInput("input", collection=False)},
        )
        with (
            patch.object(adapter, "_detect_version", return_value="2.16.0"),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            handle = adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={"threads": "4"},
                work_dir=tmp_path,
            )
        assert handle.executor == "galaxy"
        assert handle.run_id == "12345"
        assert handle.status == "running"
        mock_popen.assert_called_once()
        cmd_args = mock_popen.call_args[0][0]
        rendered = " ".join(cmd_args)
        assert "/data/file.bam" in rendered
        assert str(tmp_path) in rendered

    def test_submit_spec_no_command_raises(self, tmp_path):
        """submit_spec() should raise if spec has no command."""
        adapter = GalaxyAdapter()
        spec = _MockSpec(command="")
        with pytest.raises(RuntimeError, match="no command field"):
            adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={},
                work_dir=tmp_path,
            )
