"""Regression tests for review-round fixes.

Covers:
- _discover_outputs excludes _pre_snapshot.json and log files even with
  broad declared patterns (T1/S1).
- run_tool timeout marks entry as failed with exit_code=-1 (S4/T3).
- crystallize filters out running/unknown/pending entries (T4/S5).
- _discover_outputs directory children respect exclude_paths and
  staged_input_paths (U1).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bioledger.forges.analysisforge.crystallize import (
    _build_dag,
    to_nextflow_from_entries,
)
from bioledger.forges.analysisforge.executor import (
    _discover_outputs,
    _snapshot_dir,
    run_tool,
)
from bioledger.core.containers.docker import RunResult  # noqa: F401
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    SpecStatus,
    ToolOutput,
    ToolSpec,
)


class TestDiscoverOutputsExclusion:
    """T1/S1/U1: _pre_snapshot.json, logs, and staged inputs must not
    leak as outputs even when a broad pattern is declared."""

    def test_pre_snapshot_excluded_with_wildcard_pattern(self, tmp_path: Path):
        """A `*` output pattern should not pick up _pre_snapshot.json."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # Take snapshot of empty dir
        pre_snapshot = _snapshot_dir(run_dir)

        # Simulate files written after snapshot (by the tool / framework)
        (run_dir / "_pre_snapshot.json").write_text(json.dumps(pre_snapshot))
        (run_dir / "result.txt").write_text("output")

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="test",
                container="alpine:latest",
                command="echo hi",
                inputs={},
                outputs={"out": ToolOutput(pattern="*")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        exclude = {str((run_dir / "_pre_snapshot.json").resolve())}
        outputs = _discover_outputs(
            spec, run_dir, pre_snapshot, set(), exclude_paths=exclude
        )
        output_names = [p.name for p in outputs]
        assert "_pre_snapshot.json" not in output_names
        assert "result.txt" in output_names

    def test_log_files_excluded_with_wildcard_pattern(self, tmp_path: Path):
        """stdout.log and stderr.log should not appear as outputs."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        (run_dir / "stdout.log").write_text("stdout")
        (run_dir / "stderr.log").write_text("stderr")
        (run_dir / "real_output.bam").write_text("data")

        pre_snapshot: dict[str, float] = {}

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="test",
                container="alpine:latest",
                command="echo hi",
                inputs={},
                outputs={"out": ToolOutput(pattern="*")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        exclude = {
            str((run_dir / "stdout.log").resolve()),
            str((run_dir / "stderr.log").resolve()),
        }
        outputs = _discover_outputs(
            spec, run_dir, pre_snapshot, set(), exclude_paths=exclude
        )
        output_names = [p.name for p in outputs]
        assert "stdout.log" not in output_names
        assert "stderr.log" not in output_names
        assert "real_output.bam" in output_names

    def test_directory_children_exclude_staged_inputs(self, tmp_path: Path):
        """U1: staged input files inside a matched directory should not
        appear as outputs."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # Simulate a directory output containing a staged input
        out_dir = run_dir / "results"
        out_dir.mkdir()
        (out_dir / "real_output.txt").write_text("output")
        (out_dir / "staged_input.txt").write_text("input data")

        pre_snapshot: dict[str, float] = {}

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="test",
                container="alpine:latest",
                command="echo hi",
                inputs={},
                outputs={"out": ToolOutput(pattern="results/")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        staged = {str((out_dir / "staged_input.txt").resolve())}
        outputs = _discover_outputs(
            spec, run_dir, pre_snapshot, staged, exclude_paths=set()
        )
        output_names = [p.name for p in outputs]
        assert "staged_input.txt" not in output_names
        assert "real_output.txt" in output_names


class TestRunToolTimeout:
    """S4/T3: run_tool timeout should mark entry as failed."""

    def test_timeout_marks_entry_failed(self, tmp_path: Path):
        session = LedgerSession(name="timeout-test")
        session_dir = tmp_path / "sessions" / session.id

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="slow-tool",
                container="alpine:latest",
                command="sleep 999",
                inputs={},
                outputs={"out": ToolOutput(pattern="output.txt")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        mock_runner = MagicMock()
        mock_runner.submit.return_value = "fake-container-id"
        mock_runner.poll.return_value = None  # always "still running"
        mock_runner.client.containers.get.return_value = MagicMock()

        with patch(
            "bioledger.forges.analysisforge.executor.DockerRunner",
            return_value=mock_runner,
        ):
            with pytest.raises(TimeoutError):
                run_tool(
                    session=session,
                    spec=spec,
                    input_files={},
                    session_dir=session_dir,
                    timeout=0,  # immediate timeout
                    poll_interval=0.01,
                )

        entry = session.entries[-1]
        assert entry.run_status == "failed"
        assert entry.exit_code == -1

    def test_timeout_persists_docker_logs_as_filerefs(self, tmp_path: Path):
        """Timeout must capture container logs before kill/remove."""
        session = LedgerSession(name="timeout-logs")
        session_dir = tmp_path / "sessions" / session.id

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="slow-tool",
                container="alpine:latest",
                command="sleep 999",
                inputs={},
                outputs={"out": ToolOutput(pattern="output.txt")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        container = MagicMock()

        def _logs(stdout=True, stderr=False):
            return b"partial stdout\n" if stdout else b"partial stderr\n"

        container.logs.side_effect = _logs
        mock_runner = MagicMock()
        mock_runner.submit.return_value = "fake-container-id"
        mock_runner.poll.return_value = None
        mock_runner.client.containers.get.return_value = container

        with patch(
            "bioledger.forges.analysisforge.executor.DockerRunner",
            return_value=mock_runner,
        ):
            with pytest.raises(TimeoutError):
                run_tool(
                    session=session,
                    spec=spec,
                    input_files={},
                    session_dir=session_dir,
                    timeout=0,
                    poll_interval=0.01,
                )

        entry = session.entries[-1]
        assert entry.run_status == "failed"

        run_dir = session_dir / "runs" / entry.id
        assert (run_dir / "stdout.log").read_text() == "partial stdout\n"
        assert (run_dir / "stderr.log").read_text() == "partial stderr\n"

        log_refs = {Path(f.path).name for f in entry.files if f.role == "log"}
        assert log_refs == {"stdout.log", "stderr.log"}
        # Logs captured before the container is killed/removed
        container.logs.assert_called()
        container.kill.assert_called()
        container.remove.assert_called()

    def test_timeout_persists_local_process_logs(self, tmp_path: Path):
        """Conda/local timeout: existing stdout.log in run_dir is kept."""
        from bioledger.core.conda_runner import CondaRunner

        session = LedgerSession(name="timeout-local")
        session_dir = tmp_path / "sessions" / session.id

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="slow-conda-tool",
                executors=["conda"],
                conda={"dependencies": ["python=3.11"]},
                command="sleep 999",
                inputs={},
                outputs={"out": ToolOutput(pattern="output.txt")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        def _fake_submit(conda_env, command, workdir):
            # Simulate the runner having already written partial logs
            Path(workdir).mkdir(parents=True, exist_ok=True)
            (Path(workdir) / "stdout.log").write_text(
                "local partial stdout\n"
            )
            (Path(workdir) / "stderr.log").write_text(
                "local partial stderr\n"
            )
            return "99999"

        with patch.object(CondaRunner, "ensure_env", return_value="env_x"), \
             patch.object(CondaRunner, "submit", side_effect=_fake_submit), \
             patch.object(CondaRunner, "poll", return_value=None), \
             patch(
                 "bioledger.forges.analysisforge.executor.os.killpg"
             ), patch(
                 "bioledger.forges.analysisforge.executor.os.getpgid"
             ):
            with pytest.raises(TimeoutError):
                run_tool(
                    session=session,
                    spec=spec,
                    input_files={},
                    session_dir=session_dir,
                    params={},
                    timeout=0,
                    poll_interval=0.01,
                )

        entry = session.entries[-1]
        assert entry.run_status == "failed"

        log_refs = {Path(f.path).name for f in entry.files if f.role == "log"}
        assert "stdout.log" in log_refs
        assert "stderr.log" in log_refs

        run_dir = session_dir / "runs" / entry.id
        assert "local partial stdout" in (run_dir / "stdout.log").read_text()

    def test_timeout_message_includes_stderr_tail(self, tmp_path: Path):
        """TimeoutError should surface the last stderr lines for diagnosis."""
        session = LedgerSession(name="timeout-tail")
        session_dir = tmp_path / "sessions" / session.id

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="slow-tool",
                container="alpine:latest",
                command="sleep 999",
                inputs={},
                outputs={"out": ToolOutput(pattern="output.txt")},
                parameters={},
                status=SpecStatus.VALID,
            ),
        )

        container = MagicMock()
        container.logs.side_effect = (
            lambda stdout=True, stderr=False: (
                b"" if stdout else b"line1\nline2\nFATAL: out of memory\n"
            )
        )
        mock_runner = MagicMock()
        mock_runner.submit.return_value = "fake-container-id"
        mock_runner.poll.return_value = None
        mock_runner.client.containers.get.return_value = container

        with patch(
            "bioledger.forges.analysisforge.executor.DockerRunner",
            return_value=mock_runner,
        ):
            with pytest.raises(TimeoutError, match="FATAL: out of memory"):
                run_tool(
                    session=session,
                    spec=spec,
                    input_files={},
                    session_dir=session_dir,
                    timeout=0,
                    poll_interval=0.01,
                )


class TestCrystallizeFiltering:
    """T4/S5: crystallize should only include completed/failed entries."""

    def test_build_dag_skips_running_entries(self):
        session = LedgerSession()
        completed = LedgerEntry(
            id="e1",
            kind=EntryKind.TOOL_RUN,
            run_status="completed",
            tool_spec_name="tool-a",
        )
        running = LedgerEntry(
            id="e2",
            kind=EntryKind.TOOL_RUN,
            run_status="running",
            tool_spec_name="tool-b",
            parent_id="e1",
        )
        unknown = LedgerEntry(
            id="e3",
            kind=EntryKind.TOOL_RUN,
            run_status="unknown",
            tool_spec_name="tool-c",
        )
        session.entries = [completed, running, unknown]

        children, by_id = _build_dag(session)
        assert "e1" in by_id
        assert "e2" not in by_id
        assert "e3" not in by_id

    def test_to_nextflow_from_entries_skips_non_terminal(self):
        completed = LedgerEntry(
            id="e1",
            kind=EntryKind.TOOL_RUN,
            run_status="completed",
            tool_spec_name="tool-a",
            container=ContainerInfo(image="alpine:latest", command=["echo"]),
            files=[FileRef(path="/tmp/out.txt", sha256="abc", size_bytes=10, role="output")],
        )
        running = LedgerEntry(
            id="e2",
            kind=EntryKind.TOOL_RUN,
            run_status="running",
            tool_spec_name="tool-b",
            container=ContainerInfo(image="alpine:latest", command=["echo"]),
        )
        nf = to_nextflow_from_entries([completed, running])
        assert "tool_a" in nf
        assert "tool_b" not in nf
