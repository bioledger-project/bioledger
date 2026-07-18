"""Tests for log persistence, output exclusion, and tail-biased failure snippets.

Covers the fix where container stdout/stderr were never persisted to disk and
only a 400-char head-slice of stderr was shown to the user/LLM, hiding the
actual error for tools like GATK that put the real failure near the end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bioledger.apps.cli.main import _failure_snippet
from bioledger.core.containers.docker import RunResult
from bioledger.forges.analysisforge.executor import (
    _discover_outputs,
    _persist_logs,
    run_tool,
)
from bioledger.ledger.models import LedgerSession
from bioledger.toolspec.models import (
    ExecutionSpec,
    SpecStatus,
    ToolOutput,
    ToolSpec,
)


class TestPersistLogs:
    """Tests for _persist_logs helper."""

    def test_writes_stdout_and_stderr(self, tmp_path: Path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        result = RunResult(
            exit_code=1,
            stdout="some stdout\n",
            stderr="some stderr\n",
            duration_seconds=0.1,
        )
        log_paths = _persist_logs(run_dir, result)

        assert (run_dir / "stdout.log").exists()
        assert (run_dir / "stderr.log").exists()
        assert (run_dir / "stdout.log").read_text() == "some stdout\n"
        assert (run_dir / "stderr.log").read_text() == "some stderr\n"
        assert len(log_paths) == 2
        assert all(".log" in p for p in log_paths)

    def test_returns_resolved_paths(self, tmp_path: Path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        result = RunResult(
            exit_code=0, stdout="", stderr="", duration_seconds=0.0
        )
        log_paths = _persist_logs(run_dir, result)

        for p in log_paths:
            assert Path(p).is_absolute()


class TestDiscoverOutputsExcludesLogs:
    """Tests that _discover_outputs skip stdout.log/stderr.log via exclude_paths."""

    def test_logs_not_emitted_as_outputs(self, tmp_path: Path):
        """Log files written after the pre_snapshot must not leak into outputs."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # Take snapshot (empty dir) before any files are created.
        # Since the tool has NO declared output patterns, fallback should fire.

        # Take snapshot (empty dir)
        from bioledger.forges.analysisforge.executor import _snapshot_dir
        pre_snapshot = _snapshot_dir(run_dir)

        # Now simulate "after run": write a log file and an actual output
        (run_dir / "stdout.log").write_text("log content")
        (run_dir / "result.txt").write_text("real output")

        # Take post-snapshot
        post_snapshot = _snapshot_dir(run_dir)
        # Verify result.txt has a newer mtime than pre_snapshot
        result_key = str((run_dir / "result.txt").resolve())
        log_key = str((run_dir / "stdout.log").resolve())
        assert result_key in post_snapshot
        assert log_key in post_snapshot

        # Build exclusion set for log files
        exclude_paths = {log_key}

        spec = ToolSpec(
            name="no-pattern",
            version="1.0",
            status=SpecStatus.VALID,
            execution=ExecutionSpec(
                name="no-pattern",
                container="alpine",
                command="echo hi",
                inputs={},
                outputs={},  # no declared patterns
                parameters={},
            ),
        )

        output_paths = _discover_outputs(
            spec, run_dir, pre_snapshot, set(), exclude_paths=exclude_paths
        )
        names = {p.name for p in output_paths}
        assert "result.txt" in names
        assert "stdout.log" not in names


class TestFailureSnippet:
    """Tests for _failure_snippet tail-biased truncation."""

    def test_tail_bias_includes_real_error(self):
        """A long stderr with a banner at start and error at end should show the error."""
        banner = "INFO  NativeLibraryLoader - Loading libgkl_compression.so from jar file\n" * 60
        real_error = "A USER ERROR has occurred: reference dictionary is not valid\n"
        stderr = banner + real_error
        assert len(stderr) > 3000

        result = SimpleNamespace(stderr=stderr, stdout="")
        snippet = _failure_snippet(result, Path("/fake/run_dir"))

        assert real_error in snippet
        # The very first banner line had no unique marker; instead verify the
        # HEAD of the text was cut off by putting a unique sentinel there.
        head_sentinel = "THIS_IS_THE_FIRST_LINE_OF_STDERR"
        stderr_with_sentinel = head_sentinel + "\n" + stderr
        result2 = SimpleNamespace(stderr=stderr_with_sentinel, stdout="")
        snippet2 = _failure_snippet(result2, Path("/fake/run_dir"))
        assert head_sentinel not in snippet2
        assert "[showing last 3000 chars of stderr" in snippet

    def test_fallback_to_stdout_when_stderr_empty(self):
        result = SimpleNamespace(stderr="  ", stdout="stdout-only content")
        snippet = _failure_snippet(result, Path("/fake/run_dir"))
        assert "stdout-only content" in snippet
        assert "stdout" in snippet

    def test_no_truncation_when_under_limit(self):
        text = "short error"
        result = SimpleNamespace(stderr=text, stdout="")
        snippet = _failure_snippet(result, Path("/fake/run_dir"))
        assert snippet == text
        assert "[showing last" not in snippet

    def test_no_output_returns_placeholder(self):
        result = SimpleNamespace(stderr="", stdout="")
        snippet = _failure_snippet(result, Path("/fake/run_dir"))
        assert snippet == "(no output captured)"

    def test_log_path_note_in_truncated_snippet(self):
        long_text = "x" * 5000
        result = SimpleNamespace(stderr=long_text, stdout="")
        snippet = _failure_snippet(result, Path("/fake/run_dir"), max_chars=100)
        assert "[showing last 100 chars of stderr" in snippet
        assert "full log: /fake/run_dir/stderr.log" in snippet


class TestRunToolLogFileRefs:
    """Integration-level test: run_tool with mocked runner attaches log FileRefs."""

    def test_log_file_refs_attached(self, tmp_path: Path):
        session = LedgerSession()
        session_dir = tmp_path / "sessions" / session.id
        spec = ToolSpec(
            name="mock-test",
            version="1.0",
            status=SpecStatus.VALID,
            execution=ExecutionSpec(
                name="mock-test",
                container="mock:latest",
                command="echo hello",
                inputs={},
                outputs={"out": ToolOutput(pattern="out.txt")},
                parameters={},
            ),
        )

        mock_result = RunResult(
            exit_code=1,
            stdout="stdout content\n",
            stderr="stderr content\n",
            duration_seconds=0.5,
        )

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.submit.return_value = "fake-container-id"
            instance.poll.return_value = mock_result

            entry, result = run_tool(
                session=session,
                spec=spec,
                input_files={},
                session_dir=session_dir,
                params={},
            )

        assert result.exit_code == 1

        run_dir = session_dir / "runs" / entry.id
        assert (run_dir / "stdout.log").read_text() == "stdout content\n"
        assert (run_dir / "stderr.log").read_text() == "stderr content\n"

        log_refs = [f for f in entry.files if f.role == "log"]
        assert len(log_refs) == 2
        assert any("stdout.log" in f.path for f in log_refs)
        assert any("stderr.log" in f.path for f in log_refs)

        # Ensure logs did NOT leak into role="output"
        output_refs = [f for f in entry.files if f.role == "output"]
        assert not any("stdout.log" in f.path for f in output_refs)
        assert not any("stderr.log" in f.path for f in output_refs)

    def test_log_file_refs_for_run_script(self, tmp_path: Path):
        """run_script also persists logs and attaches FileRefs."""
        from bioledger.forges.analysisforge.executor import run_script

        session = LedgerSession()
        session_dir = tmp_path / "sessions" / session.id
        script = tmp_path / "script.py"
        script.write_text("print('hello')\n")

        mock_result = RunResult(
            exit_code=0,
            stdout="hello\n",
            stderr="",
            duration_seconds=0.1,
        )

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.submit.return_value = "fake-container-id"
            instance.poll.return_value = mock_result

            entry, result = run_script(
                session=session,
                script_path=script,
                session_dir=session_dir,
            )

        run_dir = session_dir / "runs" / entry.id
        assert (run_dir / "stdout.log").read_text() == "hello\n"

        log_refs = [f for f in entry.files if f.role == "log"]
        assert len(log_refs) == 2  # stdout.log exists, stderr.log written as empty
