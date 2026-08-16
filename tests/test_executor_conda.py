"""Tests for conda branching in executor.py — _resolve_runner_kind, poll_tool, run_tool."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.conda_runner import CondaRunner
from bioledger.core.containers.docker import DockerRunner, RunResult
from bioledger.forges.analysisforge.executor import (
    _resolve_runner_kind,
    _poll_runner,
    poll_tool,
    run_tool,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    LedgerEntry,
    LedgerSession,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def _make_conda_spec() -> ToolSpec:
    return ToolSpec(
        name="samtools-sort-conda",
        status=SpecStatus.VALID,
        execution=ExecutionSpec(
            name="samtools_sort_conda",
            executors=["conda"],
            conda_deps=["samtools=1.17"],
            command="samtools sort -o {{outputs._dir}}/sorted.bam {{inputs.input_bam}}",
            inputs={"input_bam": ToolInput(type="file", format="bam")},
            outputs={"sorted": ToolOutput(pattern="sorted.bam")},
        ),
    )


def _make_docker_spec() -> ToolSpec:
    return ToolSpec(
        name="echo-docker",
        status=SpecStatus.VALID,
        execution=ExecutionSpec(
            name="echo_docker",
            executors=["docker"],
            container="alpine:latest",
            command="echo hello > {{outputs._dir}}/output.txt",
            inputs={},
            outputs={"out": ToolOutput(pattern="output.txt")},
        ),
    )


def _make_engine_only_spec() -> ToolSpec:
    return ToolSpec(
        name="nf-only",
        status=SpecStatus.VALID,
        execution=ExecutionSpec(
            name="nf_only",
            executors=["nextflow"],
            command="echo hello",
            inputs={},
            outputs={},
        ),
    )


class TestResolveRunnerKind:
    def test_docker_spec(self):
        assert _resolve_runner_kind(_make_docker_spec()) == "docker"

    def test_conda_spec(self):
        assert _resolve_runner_kind(_make_conda_spec()) == "conda"

    def test_raises_for_engine_only_spec(self):
        with pytest.raises(ValueError, match="no docker/conda executor"):
            _resolve_runner_kind(_make_engine_only_spec())

    def test_docker_conda_mix_prefers_docker(self):
        spec = ToolSpec(
            name="mixed",
            execution=ExecutionSpec(
                name="mixed",
                executors=["docker", "conda"],
                container="alpine:latest",
                conda_deps=["samtools=1.17"],
                command="echo hi",
            ),
        )
        assert _resolve_runner_kind(spec) == "docker"


class TestPollRunner:
    def test_docker_uses_poll_directly(self):
        runner = MagicMock(spec=DockerRunner)
        runner.poll.return_value = RunResult(exit_code=0, stdout="", stderr="", duration_seconds=0.0)
        result = _poll_runner(runner, "container_id", Path("/tmp"))
        runner.poll.assert_called_once_with("container_id")
        assert result.exit_code == 0

    def test_conda_passes_workdir(self):
        runner = MagicMock(spec=CondaRunner)
        runner.poll.return_value = RunResult(exit_code=0, stdout="", stderr="", duration_seconds=0.0)
        result = _poll_runner(runner, "12345", Path("/tmp/run"), expected_start_time=123.0)
        runner.poll.assert_called_once_with("12345", Path("/tmp/run"), 123.0)
        assert result.exit_code == 0


class TestPollToolConda:
    def test_poll_tool_conda_uses_conda_runner(self, tmp_path):
        """poll_tool should use CondaRunner for conda entries, not DockerRunner."""
        session_dir = tmp_path / "sessions" / "test"
        run_dir = session_dir / "runs" / "test123"
        run_dir.mkdir(parents=True)

        entry = LedgerEntry(
            id="test123",
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="samtools_sort_conda",
            tool_spec_snapshot={
                "name": "samtools_sort_conda",
                "executors": ["conda"],
                "conda_deps": ["samtools=1.17"],
                "command": "samtools sort",
                "inputs": {},
                "outputs": {},
                "status": "valid",
            },
            container=ContainerInfo(
                image="conda:bioledger_abc",
                command=["samtools", "sort"],
                input_mapping={},
            ),
            files=[],
            run_status="running",
            run_handle_id="12345",
            executor="conda",
        )

        with patch.object(CondaRunner, "poll", return_value=RunResult(
            exit_code=0, stdout="", stderr="", duration_seconds=1.0
        )) as mock_poll:
            result = poll_tool(entry, session_dir)

            mock_poll.assert_called_once()
            call_args = mock_poll.call_args
            assert call_args.args[0] == "12345"
            assert call_args.args[1] == run_dir

        assert result.run_status == "completed"
        assert result.exit_code == 0

    def test_poll_tool_reads_persisted_start_time(self, tmp_path):
        """poll_tool should read _process_start_time.json and pass it to CondaRunner.poll."""
        session_dir = tmp_path / "sessions" / "test"
        run_dir = session_dir / "runs" / "test456"
        run_dir.mkdir(parents=True)

        # Write the sidecar
        expected_start = 99999.0
        (run_dir / "_process_start_time.json").write_text(json.dumps(expected_start))

        entry = LedgerEntry(
            id="test456",
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="samtools_sort_conda",
            tool_spec_snapshot={
                "name": "samtools_sort_conda",
                "executors": ["conda"],
                "conda_deps": ["samtools=1.17"],
                "command": "samtools sort",
                "inputs": {},
                "outputs": {},
                "status": "valid",
            },
            container=ContainerInfo(
                image="conda:bioledger_abc",
                command=["samtools", "sort"],
                input_mapping={},
            ),
            files=[],
            run_status="running",
            run_handle_id="67890",
            executor="conda",
        )

        with patch.object(CondaRunner, "poll", return_value=RunResult(
            exit_code=0, stdout="", stderr="", duration_seconds=1.0
        )) as mock_poll:
            poll_tool(entry, session_dir)

            call_args = mock_poll.call_args
            assert call_args.args[2] == expected_start


class TestRunToolConda:
    def test_run_tool_conda_branches_correctly(self, tmp_path):
        """run_tool should use CondaRunner for conda specs, not DockerRunner."""
        session = LedgerSession(name="conda-test")
        session_dir = tmp_path / "sessions" / session.id

        spec = _make_conda_spec()

        with patch.object(CondaRunner, "ensure_env", return_value="bioledger_abc123"), \
             patch.object(CondaRunner, "submit", return_value="99999") as mock_submit, \
             patch.object(CondaRunner, "poll", return_value=RunResult(
                 exit_code=0, stdout="ok", stderr="", duration_seconds=0.5
             )) as mock_poll, \
             patch("psutil.Process") as mock_proc_cls:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = 123.0
            mock_proc_cls.return_value = mock_proc

            entry, result = run_tool(
                session=session,
                spec=spec,
                input_files={},
                session_dir=session_dir,
                params={},
            )

            # submit was called with env name and workdir
            assert mock_submit.called
            submit_kwargs = mock_submit.call_args.kwargs
            assert submit_kwargs["conda_env"] == "bioledger_abc123"
            # poll was called (via _poll_runner, which passes workdir)
            assert mock_poll.called
            poll_args = mock_poll.call_args
            assert poll_args.args[1] == session_dir / "runs" / entry.id

        assert result.exit_code == 0
        assert entry.executor == "conda"
        assert entry.container.image.startswith("conda:")
        # No _process_start_time.json in entry.files (sidecar exclusion)
        sidecar_names = [Path(f.path).name for f in entry.files]
        assert "_process_start_time.json" not in sidecar_names

    @patch("bioledger.forges.analysisforge.executor.os.kill")
    @patch("bioledger.forges.analysisforge.executor.os.killpg")
    @patch("bioledger.forges.analysisforge.executor.os.getpgid")
    def test_run_tool_conda_timeout_kills_process_group(self, mock_getpgid, mock_killpg, mock_kill, tmp_path):
        """Timeout in conda run_tool should kill the process group, not use Docker kill."""
        session = LedgerSession(name="timeout-test")
        session_dir = tmp_path / "sessions" / session.id

        spec = _make_conda_spec()

        with patch.object(CondaRunner, "ensure_env", return_value="bioledger_xyz"), \
             patch.object(CondaRunner, "submit", return_value="99999"), \
             patch.object(CondaRunner, "poll", return_value=None), \
             patch("psutil.Process") as mock_proc_cls:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = 123.0
            mock_proc_cls.return_value = mock_proc

            with pytest.raises(TimeoutError, match="exceeded timeout"):
                run_tool(
                    session=session,
                    spec=spec,
                    input_files={},
                    session_dir=session_dir,
                    params={},
                    timeout=0,  # immediate timeout
                    poll_interval=0.01,
                )

            mock_getpgid.assert_called()
            mock_killpg.assert_called()
