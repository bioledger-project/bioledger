"""Tests for CondaRunner — native conda execution via conda run."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.conda_runner import (
    CondaRunner,
    _conda_binary,
    _env_name,
    _wrap_for_exit_code,
)


class TestEnvName:
    def test_env_name_deterministic(self):
        name1 = _env_name(["samtools=1.17"])
        name2 = _env_name(["samtools=1.17"])
        assert name1 == name2
        assert name1.startswith("bioledger_")

    def test_env_name_handles_ordering(self):
        name1 = _env_name(["samtools=1.17", "bwa=0.7.17"])
        name2 = _env_name(["bwa=0.7.17", "samtools=1.17"])
        assert name1 == name2

    def test_env_name_different_deps(self):
        name1 = _env_name(["samtools=1.17"])
        name2 = _env_name(["bwa=0.7.17"])
        assert name1 != name2


class TestCondaBinary:
    @patch("bioledger.core.conda_runner.shutil.which")
    def test_prefers_mamba(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/bin/mamba" if x == "mamba" else None
        assert _conda_binary() == "/usr/bin/mamba"

    @patch("bioledger.core.conda_runner.shutil.which")
    def test_falls_back_to_conda(self, mock_which):
        mock_which.side_effect = lambda x: None if x == "mamba" else "/opt/conda/bin/conda"
        assert _conda_binary() == "/opt/conda/bin/conda"

    @patch("bioledger.core.conda_runner.shutil.which")
    def test_raises_if_neither(self, mock_which):
        mock_which.return_value = None
        with pytest.raises(RuntimeError, match="conda/mamba not found"):
            _conda_binary()


class TestEnsureEnv:
    @patch("bioledger.core.conda_runner.subprocess.run")
    def test_reuses_existing(self, mock_run):
        env_name = _env_name(["samtools=1.17"])
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"envs": [f"/opt/conda/envs/{env_name}"]})
        )
        runner = CondaRunner()
        result = runner.ensure_env(["samtools=1.17"])
        assert result == env_name
        # Only env list --json should be called, not create
        assert mock_run.call_count == 1
        args = mock_run.call_args_list[0][0][0]
        assert "env" in args and "list" in args

    @patch("bioledger.core.conda_runner.subprocess.run")
    def test_creates_if_missing(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps({"envs": []}))
        runner = CondaRunner()
        result = runner.ensure_env(["samtools=1.17"])
        assert result.startswith("bioledger_")
        assert mock_run.call_count == 2
        create_args = mock_run.call_args_list[1][0][0]
        assert "create" in create_args
        assert "-y" in create_args

    @patch("bioledger.core.conda_runner.subprocess.run")
    def test_matches_basename_not_full_path(self, mock_run):
        env_name = _env_name(["samtools=1.17"])
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"envs": [f"/opt/conda/envs/{env_name}"]})
        )
        runner = CondaRunner()
        result = runner.ensure_env(["samtools=1.17"])
        assert result == env_name


class TestWrapForExitCode:
    def test_plain_argv(self):
        cmd = ["samtools", "sort", "-o", "out.bam"]
        wrapped = _wrap_for_exit_code(cmd)
        assert wrapped[0] == "sh"
        assert wrapped[1] == "-c"
        # EXIT trap + env-var destination — survives `exit N` inside the
        # command and paths with spaces/quotes.
        assert 'echo $? > "$BIOLEDGER_EXIT_CODE_PATH"' in wrapped[2]
        assert wrapped[2].startswith("trap '")
        # Round-trip: the original command should be in the wrapped string
        assert "samtools sort -o out.bam" in wrapped[2]

    def test_shell_wrapped(self):
        cmd = ["sh", "-c", "a | b"]
        wrapped = _wrap_for_exit_code(cmd)
        assert wrapped[0] == "sh"
        assert wrapped[1] == "-c"
        # Should unwrap and re-wrap the raw string, not double-nest
        assert wrapped[2] == (
            "trap 'echo $? > \"$BIOLEDGER_EXIT_CODE_PATH\"' EXIT\na | b"
        )


class TestSubmit:
    @patch("bioledger.core.conda_runner.subprocess.Popen")
    @patch("bioledger.core.conda_runner._conda_binary", return_value="conda")
    def test_launches_subprocess(self, mock_binary, mock_popen, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        runner = CondaRunner()
        pid_str = runner.submit(
            conda_env="bioledger_abc",
            command=["samtools", "sort", "-o", "out.bam"],
            workdir=tmp_path,
        )

        assert pid_str == "12345"
        # Verify Popen was called with start_new_session=True
        assert mock_popen.call_args.kwargs["start_new_session"] is True
        assert mock_popen.call_args.kwargs["cwd"] == str(tmp_path)
        # Verify argv includes conda run
        argv = mock_popen.call_args.args[0]
        assert "conda" in argv
        assert "run" in argv
        assert "-n" in argv
        assert "bioledger_abc" in argv

    @patch("bioledger.core.conda_runner.subprocess.Popen")
    @patch("bioledger.core.conda_runner._conda_binary", return_value="conda")
    def test_closes_log_file_handles(self, mock_binary, mock_popen, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_popen.return_value = mock_proc

        runner = CondaRunner()
        runner.submit(
            conda_env="bioledger_test",
            command=["echo", "hello"],
            workdir=tmp_path,
        )

        # Verify stdout.log and stderr.log were created
        assert (tmp_path / "stdout.log").exists()
        assert (tmp_path / "stderr.log").exists()
        # The file handles should be closed (Popen got them, parent closed them)
        # We can't directly check FD state, but verify no exception and files exist


class TestPoll:
    @patch("bioledger.core.conda_runner._is_process_alive", return_value=True)
    def test_returns_none_if_alive(self, mock_alive, tmp_path):
        runner = CondaRunner()
        result = runner.poll("12345", tmp_path)
        assert result is None

    @patch("bioledger.core.conda_runner._is_process_alive", return_value=False)
    def test_returns_result_if_dead(self, mock_alive, tmp_path):
        (tmp_path / ".exit_code").write_text("0")
        (tmp_path / "stdout.log").write_text("output")
        (tmp_path / "stderr.log").write_text("errors")

        runner = CondaRunner()
        result = runner.poll("12345", tmp_path)
        assert result is not None
        assert result.exit_code == 0
        assert result.stdout == "output"
        assert result.stderr == "errors"

    @patch("bioledger.core.conda_runner._is_process_alive", return_value=False)
    def test_missing_exit_code_returns_synthetic_failure(self, mock_alive, tmp_path):
        (tmp_path / "stdout.log").write_text("partial")
        (tmp_path / "stderr.log").write_text("")

        runner = CondaRunner()
        result = runner.poll("12345", tmp_path)
        assert result is not None
        assert result.exit_code == -1
        assert "terminated without writing exit code" in result.stderr

    @patch("bioledger.core.conda_runner._is_process_alive", return_value=False)
    def test_works_across_instances(self, mock_alive, tmp_path):
        """poll should work on a fresh CondaRunner instance (cross-process)."""
        (tmp_path / ".exit_code").write_text("0")
        (tmp_path / "stdout.log").write_text("")
        (tmp_path / "stderr.log").write_text("")

        runner2 = CondaRunner()
        result = runner2.poll("12345", tmp_path)
        assert result is not None
        assert result.exit_code == 0
