"""Non-mocked tests for the shared engine subprocess layer (_subprocess.py).

These exercise real ``/bin/sh`` launches: EXIT-trap sidecar semantics,
zombie detection, and poll_local_process's sidecar/fallback ordering.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bioledger.core.engines._subprocess import (
    _OutputRef,
    launch_logged_process,
    log_fallback,
    poll_local_process,
    read_exit_code,
    render_and_launch,
)
from bioledger.core.engines.protocol import RunHandle, _is_process_alive


def _wait_for_exit_code(work_dir: Path, timeout: float = 10.0) -> int:
    """Poll for the .exit_code sidecar and return its value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = read_exit_code(work_dir)
        if code is not None:
            return code
        time.sleep(0.05)
    pytest.fail(f".exit_code never appeared in {work_dir}")


class TestLaunchLoggedProcess:
    def test_simple_command_writes_exit_code(self, tmp_path):
        handle = launch_logged_process(
            "echo hello", tmp_path, "test", log_filename="test.log"
        )
        assert _wait_for_exit_code(tmp_path) == 0
        assert "hello" in (tmp_path / "test.log").read_text()
        assert handle.status == "running"
        assert handle.run_id.isdigit()

    def test_shell_operators_and_multiline(self, tmp_path):
        """Heredoc + && + pipes must work — the old shlex.split path broke all."""
        handle = launch_logged_process(
            "cat > data.txt <<'EOF'\nline1\nline2\nEOF\n"
            "cat data.txt | grep -c line > count.txt && echo done",
            tmp_path,
            "test",
        )
        assert _wait_for_exit_code(tmp_path) == 0
        assert (tmp_path / "count.txt").read_text().strip() == "2"
        assert handle.run_id.isdigit()

    def test_work_dir_with_spaces(self, tmp_path):
        spaced = tmp_path / "dir with spaces"
        spaced.mkdir()
        handle = launch_logged_process("echo ok", spaced, "test")
        assert _wait_for_exit_code(spaced) == 0
        assert handle.run_id.isdigit()

    def test_exit_n_writes_sidecar(self, tmp_path):
        """`exit 2` at the end must still write .exit_code — the old
        `cmd; echo $?` pattern lost this when the outer shell exited first."""
        launch_logged_process("echo hi\nexit 2", tmp_path, "test")
        assert _wait_for_exit_code(tmp_path) == 2

    def test_cd_then_exit_writes_to_original_dir(self, tmp_path):
        """The sidecar must land in work_dir even when the command cd's away
        — that's why the path travels via $BIOLEDGER_EXIT_CODE_PATH."""
        launch_logged_process("cd /tmp\nexit 3", tmp_path, "test")
        assert _wait_for_exit_code(tmp_path) == 3
        # And must NOT have leaked a .exit_code into /tmp
        # (can't reliably assert absence in shared /tmp; the env var
        # resolves to the absolute work_dir path, covered above).

    def test_stale_exit_code_cleared(self, tmp_path):
        """A stale .exit_code from a previous run is removed before launch."""
        (tmp_path / ".exit_code").write_text("7")
        launch_logged_process("sleep 0.2\nexit 0", tmp_path, "test")
        assert _wait_for_exit_code(tmp_path) == 0


class TestIsProcessAliveZombie:
    def test_zombie_child_reported_dead(self):
        """A reaped-able zombie child must not report as alive."""
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        # Give the child a moment to become a zombie.
        time.sleep(0.1)
        try:
            assert _is_process_alive(pid) is False
        finally:
            # Ensure the child is reaped even on assertion failure.
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


class TestPollLocalProcess:
    def _handle(self, run_id: str, work_dir: Path) -> RunHandle:
        return RunHandle(
            executor="test",
            run_id=run_id,
            entry_id="",
            work_dir=work_dir,
        )

    def test_unparseable_run_id_returns_none(self, tmp_path):
        assert poll_local_process(self._handle("not-a-pid", tmp_path), "x.log") is None

    def test_exit_code_zero_completes(self, tmp_path):
        (tmp_path / ".exit_code").write_text("0")
        status = poll_local_process(self._handle("99999999", tmp_path), "x.log")
        assert status is not None and status.state == "completed"

    def test_exit_code_nonzero_fails(self, tmp_path):
        (tmp_path / ".exit_code").write_text("137")
        status = poll_local_process(self._handle("99999999", tmp_path), "x.log")
        assert status is not None and status.state == "failed"
        assert "137" in status.message

    def test_sidecar_wins_over_fallback(self, tmp_path):
        """.exit_code is the source of truth even when the log looks fine."""
        (tmp_path / ".exit_code").write_text("1")
        (tmp_path / "x.log").write_text("all good\n")
        status = poll_local_process(
            self._handle("99999999", tmp_path),
            "x.log",
            fallback_check=log_fallback("x.log", success_markers=("all good",)),
        )
        assert status is not None and status.state == "failed"

    def test_fallback_used_when_no_sidecar(self, tmp_path):
        (tmp_path / "x.log").write_text("Error: broke\n")
        status = poll_local_process(
            self._handle("99999999", tmp_path),
            "x.log",
            fallback_check=log_fallback("x.log"),
        )
        assert status is not None and status.state == "failed"

    def test_unknown_when_no_sidecar_no_fallback(self, tmp_path):
        status = poll_local_process(self._handle("99999999", tmp_path), "x.log")
        assert status is not None and status.state == "unknown"

    def test_running_process(self, tmp_path):
        status = poll_local_process(
            self._handle(str(os.getpid()), tmp_path), "x.log"
        )
        assert status is not None and status.state == "running"


class TestRenderAndLaunch:
    def test_required_input_missing_raises(self, tmp_path):
        class _In:
            required = True

        class _Spec:
            name = "t"
            inputs = {"reads": _In()}
            outputs = {}
            parameters = {}

        with pytest.raises(ValueError, match="reads"):
            render_and_launch(
                "echo {{ inputs.reads }}",
                input_mapping={},
                params={},
                work_dir=tmp_path,
                executor_name="test",
                spec=_Spec(),
            )

    def test_optional_input_absent_renders_empty(self, tmp_path):
        class _In:
            def __init__(self, required):
                self.required = required

        class _Spec:
            name = "t"
            inputs = {"req": _In(True), "opt": _In(False)}
            outputs = {}
            parameters = {}

        handle = render_and_launch(
            "echo '[{% if inputs.opt %}OPT{% else %}NONE{% endif %}]' > flag.txt",
            input_mapping={"req": "/data/x"},
            params={},
            work_dir=tmp_path,
            executor_name="test",
            spec=_Spec(),
        )
        assert _wait_for_exit_code(tmp_path) == 0
        assert handle.run_id.isdigit()
        assert "NONE" in (tmp_path / "flag.txt").read_text()

    def test_outputs_name_path_renders_pattern(self, tmp_path):
        class _Out:
            pattern = "result.bam"

        class _Spec:
            name = "t"
            inputs = {}
            outputs = {"bam": _Out()}
            parameters = {}

        handle = render_and_launch(
            "echo {{ outputs.bam.path }} > p1.txt; echo {{ outputs.bam }} > p2.txt",
            input_mapping={},
            params={},
            work_dir=tmp_path,
            executor_name="test",
            spec=_Spec(),
        )
        assert _wait_for_exit_code(tmp_path) == 0
        expected = str(tmp_path / "result.bam")
        assert (tmp_path / "p1.txt").read_text().strip() == expected
        assert (tmp_path / "p2.txt").read_text().strip() == expected
        assert handle.run_id.isdigit()

    def test_projectdir_resolves_to_work_dir(self, tmp_path):
        handle = render_and_launch(
            "echo ${projectDir}/sub > pd.txt",
            input_mapping={},
            params={},
            work_dir=tmp_path,
            executor_name="test",
        )
        assert _wait_for_exit_code(tmp_path) == 0
        assert (tmp_path / "pd.txt").read_text().strip() == str(tmp_path / "sub")
        assert handle.run_id.isdigit()


class TestFinalizeEngineEntry:
    def _make_entry(self, work_dir: Path):
        from bioledger.ledger.models import (
            ContainerInfo,
            EntryKind,
            LedgerEntry,
        )

        return LedgerEntry(
            id=work_dir.name,
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="t",
            tool_spec_snapshot={
                "name": "t",
                "version": "1.0",
                "command": "echo hi",
                "inputs": {},
                "outputs": {},
            },
            container=ContainerInfo(
                image="", command=[], input_mapping={}
            ),
            run_status="running",
            run_handle_id="99999999",
            executor="nextflow",
        )

    def test_unknown_state_finalizes_failed_no_exit_code(self, tmp_path):
        from unittest.mock import MagicMock

        from bioledger.core.engines.protocol import RunStatus
        from bioledger.forges.analysisforge.agent import _finalize_engine_entry

        adapter = MagicMock()
        adapter.collect.return_value = []
        entry = self._make_entry(tmp_path)
        handle = RunHandle(
            executor="nextflow",
            run_id="99999999",
            entry_id=entry.id,
            work_dir=tmp_path,
        )
        status = RunStatus(state="unknown", message="process gone")

        result = _finalize_engine_entry(entry, handle, tmp_path, adapter, status)
        assert result.run_status == "failed"
        assert result.exit_code is None
        assert "uncertain" in result.notes

    def test_failed_state_reads_real_exit_code(self, tmp_path):
        from unittest.mock import MagicMock

        from bioledger.core.engines.protocol import RunStatus
        from bioledger.forges.analysisforge.agent import _finalize_engine_entry

        (tmp_path / ".exit_code").write_text("137")
        adapter = MagicMock()
        adapter.collect.return_value = []
        entry = self._make_entry(tmp_path)
        handle = RunHandle(
            executor="nextflow",
            run_id="99999999",
            entry_id=entry.id,
            work_dir=tmp_path,
        )
        status = RunStatus(state="failed", message="boom")

        result = _finalize_engine_entry(entry, handle, tmp_path, adapter, status)
        assert result.run_status == "failed"
        assert result.exit_code == 137

    def test_engine_files_excluded_from_outputs(self, tmp_path):
        """Staged companions, sidecars, and engine logs must not be
        harvested as tool outputs."""
        from unittest.mock import MagicMock

        from bioledger.core.engines.protocol import RunStatus
        from bioledger.forges.analysisforge.agent import _finalize_engine_entry

        # Seed bookkeeping files + a staged companion dir + a real output.
        (tmp_path / "nextflow.log").write_text("log")
        (tmp_path / "stats.json").write_text("{}")
        (tmp_path / ".exit_code").write_text("0")
        staged = tmp_path / tmp_path.name
        staged.mkdir()
        (staged / "wrapper_main.nf").write_text("// wrapper")
        (tmp_path / "real_output.bam").write_text("bam")
        (tmp_path / "work").mkdir()
        (tmp_path / "work" / "scratch.txt").write_text("scratch")

        adapter = MagicMock()
        adapter.collect.return_value = []
        entry = self._make_entry(tmp_path)
        handle = RunHandle(
            executor="nextflow",
            run_id="99999999",
            entry_id=entry.id,
            work_dir=tmp_path,
        )
        status = RunStatus(state="completed")

        result = _finalize_engine_entry(entry, handle, tmp_path, adapter, status)
        out_paths = {f.path for f in result.files if f.role == "output"}
        assert str(tmp_path / "real_output.bam") in out_paths
        assert not any(
            "nextflow.log" in p or "stats.json" in p or ".exit_code" in p
            or "wrapper_main.nf" in p or "scratch.txt" in p
            for p in out_paths
        )


class TestShquoteFilter:
    def test_shquote_registered_in_shared_env(self):
        from bioledger.toolspec import get_jinja_env

        env = get_jinja_env()
        assert "shquote" in env.filters
        rendered = env.from_string("{{ x | shquote }}").render(
            {"x": "/path with space/f.bam"}
        )
        assert rendered == "'/path with space/f.bam'"

    def test_output_ref_is_str(self):
        ref = _OutputRef("/tmp/x.bam")
        assert isinstance(ref, str)
        assert ref == "/tmp/x.bam"
        assert ref.path == "/tmp/x.bam"
