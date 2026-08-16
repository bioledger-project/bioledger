"""Tests for unified dispatch() → facade.submit_spec() for Docker and Conda.

Verifies that ``dispatch()`` returns a facade whose ``submit_spec()`` actually
runs the tool (staging + container/conda launch + ledger entry creation),
instead of raising ``NotImplementedError``. Uses mocked runners so no real
Docker daemon or conda installation is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.dispatch import dispatch
from bioledger.core.engines.docker_facade import DockerRunnerFacade
from bioledger.core.engines.conda_facade import CondaRunnerFacade
from bioledger.core.engines.protocol import RunHandle
from bioledger.toolspec.models import (
    ExecutionSpec,
    SpecStatus,
    ToolOutput,
    ToolSpec,
)


def _make_docker_spec() -> ExecutionSpec:
    """Minimal spec with a docker executor."""
    return ExecutionSpec(
        name="echo-test",
        container="alpine:latest",
        command="echo 'hello world' > {{outputs.out.path}}",
        inputs={},
        outputs={"out": ToolOutput(pattern="output.txt")},
        parameters={},
        executors=["docker"],
        status=SpecStatus.VALID,
    )


def _make_conda_spec() -> ExecutionSpec:
    """Minimal spec with a conda executor."""
    return ExecutionSpec(
        name="echo-test-conda",
        container="alpine:latest",
        command="echo 'hello world' > {{outputs.out.path}}",
        inputs={},
        outputs={"out": ToolOutput(pattern="output.txt")},
        parameters={},
        executors=["conda"],
        conda_deps=["python=3.11"],
        status=SpecStatus.VALID,
    )


def test_dispatch_docker_returns_facade():
    """dispatch() with a docker spec returns a DockerRunnerFacade."""
    spec = _make_docker_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, DockerRunnerFacade)


def test_dispatch_conda_returns_facade():
    """dispatch() with a conda spec returns a CondaRunnerFacade."""
    spec = _make_conda_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, CondaRunnerFacade)


def test_dispatch_docker_returns_facade_and_runs(tmp_path: Path):
    """dispatch() → DockerRunnerFacade.submit_spec() runs the tool and returns a RunHandle."""
    spec = _make_docker_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, DockerRunnerFacade)

    # Set up a session dir and per-run work_dir
    session_dir = tmp_path / "sessions" / "test-session"
    work_dir = session_dir / "runs" / "test-run-id"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Mock DockerRunner so no real Docker daemon is needed
    mock_runner = MagicMock()
    mock_runner.submit.return_value = "fake-container-id-123"

    with patch(
        "bioledger.forges.analysisforge.executor.DockerRunner",
        return_value=mock_runner,
    ):
        handle = adapter.submit_spec(
            spec=spec,
            input_mapping={},
            params={},
            work_dir=work_dir,
        )

    assert isinstance(handle, RunHandle)
    assert handle.executor == "docker"
    assert handle.run_id == "fake-container-id-123"
    assert handle.entry_id  # non-empty
    assert handle.work_dir == work_dir
    # The mocked runner's submit was called with the right image
    mock_runner.submit.assert_called_once()
    call_kwargs = mock_runner.submit.call_args
    assert call_kwargs.kwargs["image"] == "alpine:latest"
    # The ledger entry is carried in metadata
    assert "ledger_entry" in handle.metadata
    entry = handle.metadata["ledger_entry"]
    assert entry.executor == "docker"
    assert entry.run_handle_id == "fake-container-id-123"
    assert entry.run_status == "running"


def test_dispatch_conda_returns_facade_and_runs(tmp_path: Path):
    """dispatch() → CondaRunnerFacade.submit_spec() runs the tool and returns a RunHandle."""
    spec = _make_conda_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, CondaRunnerFacade)

    # Set up a session dir and per-run work_dir
    session_dir = tmp_path / "sessions" / "test-session"
    work_dir = session_dir / "runs" / "test-run-id"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Mock CondaRunner so no real conda is needed
    mock_runner = MagicMock()
    mock_runner.ensure_env.return_value = "bioledger_test_env"
    mock_runner.submit.return_value = "12345"  # PID as string

    with patch(
        "bioledger.forges.analysisforge.executor.CondaRunner",
        return_value=mock_runner,
    ):
        handle = adapter.submit_spec(
            spec=spec,
            input_mapping={},
            params={},
            work_dir=work_dir,
        )

    assert isinstance(handle, RunHandle)
    assert handle.executor == "conda"
    assert handle.run_id == "12345"
    assert handle.entry_id  # non-empty
    assert handle.work_dir == work_dir
    # The mocked runner's submit was called
    mock_runner.submit.assert_called_once()
    # The ledger entry is carried in metadata
    assert "ledger_entry" in handle.metadata
    entry = handle.metadata["ledger_entry"]
    assert entry.executor == "conda"
    assert entry.run_handle_id == "12345"
    assert entry.run_status == "running"


def test_dispatch_docker_generate_raises():
    """DockerRunnerFacade.generate() still raises NotImplementedError (no fan-out)."""
    spec = _make_docker_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, DockerRunnerFacade)
    with pytest.raises(NotImplementedError, match="cannot fan-out"):
        adapter.generate(spec=spec, input_mapping={})


def test_dispatch_conda_generate_raises():
    """CondaRunnerFacade.generate() still raises NotImplementedError (no fan-out)."""
    spec = _make_conda_spec()
    adapter = dispatch(spec)
    assert isinstance(adapter, CondaRunnerFacade)
    with pytest.raises(NotImplementedError, match="cannot fan-out"):
        adapter.generate(spec=spec, input_mapping={})
