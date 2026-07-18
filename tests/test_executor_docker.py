"""Docker integration tests for the per-run-dir executor model.

These require a working Docker daemon and the `alpine:latest` image.
Skip gracefully if Docker is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bioledger.forges.analysisforge.executor import poll_tool, run_tool, submit_tool
from bioledger.ledger.models import LedgerSession
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def _make_echo_spec() -> ToolSpec:
    """Minimal spec that writes to an output file."""
    return ToolSpec(
        name="echo-test",
        version="1.0",
        status=SpecStatus.VALID,
        description="Write hello to a file.",
        execution=ExecutionSpec(
            name="echo-test",
            container="alpine:latest",
            command="echo 'hello world' > {{outputs.out.path}}",
            inputs={},
            outputs={"out": ToolOutput(pattern="output.txt")},
            parameters={},
        ),
    )


def test_run_tool_creates_isolated_run_dir(
    tmp_path: Path, docker_available: bool
):
    if not docker_available:
        pytest.skip("Docker not available")

    session = LedgerSession(name="docker-test")
    session_dir = tmp_path / "sessions" / session.id
    spec = _make_echo_spec()

    entry, result = run_tool(
        session=session,
        spec=spec,
        input_files={},
        session_dir=session_dir,
        params={},
    )

    assert result.exit_code == 0
    # The run directory was created
    assert (session_dir / "runs").exists()
    run_dirs = list((session_dir / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    # Output was discovered (logs are also tracked, so filter by role)
    output_files = [f for f in entry.files if f.role == "output"]
    assert len(output_files) == 1
    assert output_files[0].path.endswith("output.txt")

    # Output file physically exists in the run dir
    assert (run_dir / "output.txt").exists()
    assert (run_dir / "output.txt").read_text().strip() == "hello world"


def test_run_tool_stages_external_input(
    tmp_path: Path, docker_available: bool
):
    if not docker_available:
        pytest.skip("Docker not available")

    # Create an external input file
    input_file = tmp_path / "data" / "input.txt"
    input_file.parent.mkdir()
    input_file.write_text("from host\n")

    spec = ToolSpec(
        name="cat-test",
        version="1.0",
        status=SpecStatus.VALID,
        description="Copy input to output.",
        execution=ExecutionSpec(
            name="cat-test",
            container="alpine:latest",
            command="cp {{inputs.src}} {{outputs.out.path}}",
            inputs={"src": ToolInput(type=ParamType.STRING, required=True, format="text")},
            outputs={"out": ToolOutput(pattern="copied.txt")},
            parameters={},
        ),
    )

    session = LedgerSession(name="docker-test")
    session_dir = tmp_path / "sessions" / session.id

    entry, result = run_tool(
        session=session,
        spec=spec,
        input_files={"src": input_file},
        session_dir=session_dir,
        params={},
    )

    assert result.exit_code == 0
    run_dirs = list((session_dir / "runs").iterdir())
    run_dir = run_dirs[0]

    # Input was copied into the run dir
    staged_input = run_dir / "input.txt"
    assert staged_input.exists()
    assert not staged_input.is_symlink()  # external -> copy

    # Output was discovered
    assert (run_dir / "copied.txt").exists()
    assert (run_dir / "copied.txt").read_text() == "from host\n"


def test_run_tool_stages_chained_input_as_symlink(
    tmp_path: Path, docker_available: bool
):
    if not docker_available:
        pytest.skip("Docker not available")

    session = LedgerSession(name="chain-docker")
    session_dir = tmp_path / "sessions" / session.id
    session_dir.mkdir(parents=True)

    # Simulate a prior run output
    prior_run = session_dir / "runs" / "prior"
    prior_run.mkdir(parents=True)
    prior_output = prior_run / "stage1.txt"
    prior_output.write_text("chained data\n")

    spec = ToolSpec(
        name="cat-chain",
        version="1.0",
        status=SpecStatus.VALID,
        description="Read chained input and write output.",
        execution=ExecutionSpec(
            name="cat-chain",
            container="alpine:latest",
            command="cp {{inputs.data}} {{outputs.out.path}}",
            inputs={"data": ToolInput(type=ParamType.STRING, required=True, format="text")},
            outputs={"out": ToolOutput(pattern="stage2.txt")},
            parameters={},
        ),
    )

    entry, result = run_tool(
        session=session,
        spec=spec,
        input_files={"data": prior_output},
        session_dir=session_dir,
        params={},
    )

    assert result.exit_code == 0
    run_dirs = list((session_dir / "runs").iterdir())
    # Filter out the manually created "prior" run dir
    current_run = [r for r in run_dirs if r.name != "prior"][0]

    # Input was symlinked (in-session source)
    staged = current_run / "stage1.txt"
    assert staged.is_symlink()

    # Container could read the symlink and produce output
    assert (current_run / "stage2.txt").exists()
    assert (current_run / "stage2.txt").read_text() == "chained data\n"


def test_command_paths_use_container_run_dir(
    tmp_path: Path, docker_available: bool
):
    if not docker_available:
        pytest.skip("Docker not available")

    # Verify the rendered command uses /sessions/runs/<run_id>, not /work
    spec = ToolSpec(
        name="pwd-test",
        version="1.0",
        status=SpecStatus.VALID,
        description="Print working directory.",
        execution=ExecutionSpec(
            name="pwd-test",
            container="alpine:latest",
            command="pwd > {{outputs.out.path}}",
            inputs={},
            outputs={"out": ToolOutput(pattern="pwd.txt")},
            parameters={},
        ),
    )

    session = LedgerSession(name="pwd-test")
    session_dir = tmp_path / "sessions" / session.id

    entry, result = run_tool(
        session=session,
        spec=spec,
        input_files={},
        session_dir=session_dir,
        params={},
    )

    assert result.exit_code == 0
    run_dirs = list((session_dir / "runs").iterdir())
    run_dir = run_dirs[0]

    pwd_content = (run_dir / "pwd.txt").read_text().strip()
    # Should be /sessions/runs/<run_id>, not /work
    assert pwd_content.startswith("/sessions/runs/"), f"Unexpected pwd: {pwd_content}"


class TestAsyncSubmitPoll:
    """submit_tool()/poll_tool() must produce equivalent results to the
    blocking run_tool() for the same spec, and poll_tool() must work when
    called with only an entry + session_dir — as if from a fresh process."""

    def test_submit_and_poll_matches_run_tool(
        self, tmp_path: Path, docker_available: bool
    ):
        if not docker_available:
            pytest.skip("Docker not available")

        spec = _make_echo_spec()

        # Blocking path
        blocking_session = LedgerSession(name="blocking")
        blocking_dir = tmp_path / "sessions" / blocking_session.id
        blocking_entry, blocking_result = run_tool(
            session=blocking_session,
            spec=spec,
            input_files={},
            session_dir=blocking_dir,
            params={},
        )

        # Async path
        async_session = LedgerSession(name="async")
        async_dir = tmp_path / "sessions" / async_session.id
        entry = submit_tool(
            session=async_session,
            spec=spec,
            input_files={},
            session_dir=async_dir,
            params={},
        )
        assert entry.run_status == "running"
        assert entry.container_id

        import time

        while entry.run_status == "running":
            time.sleep(0.2)
            poll_tool(entry, async_dir)

        assert entry.run_status == blocking_entry.run_status == "completed"
        assert entry.exit_code == blocking_result.exit_code == 0

        def output_hashes(e):
            return {f.path.split("/")[-1]: f.sha256 for f in e.files if f.role == "output"}

        assert output_hashes(entry) == output_hashes(blocking_entry)

    def test_poll_tool_reconnects_from_fresh_entry_object(
        self, tmp_path: Path, docker_available: bool
    ):
        """Simulate a CLI process restart: only a serialized/deserialized
        entry + session_dir are available — no in-memory state from
        submit_tool() survives. poll_tool() must still finalize correctly."""
        if not docker_available:
            pytest.skip("Docker not available")

        spec = _make_echo_spec()
        session = LedgerSession(name="reconnect-test")
        session_dir = tmp_path / "sessions" / session.id

        entry = submit_tool(
            session=session,
            spec=spec,
            input_files={},
            session_dir=session_dir,
            params={},
        )

        # Round-trip through JSON to simulate a fresh process reloading the
        # entry from the LedgerStore, with no shared in-memory state.
        from bioledger.ledger.models import LedgerEntry

        reconnected_entry = LedgerEntry.model_validate_json(entry.model_dump_json())

        import time

        result = None
        deadline = time.monotonic() + 10
        while result is None and time.monotonic() < deadline:
            time.sleep(0.2)
            poll_tool(reconnected_entry, session_dir)
            if reconnected_entry.run_status != "running":
                result = reconnected_entry

        assert result is not None
        assert result.run_status == "completed"
        assert result.exit_code == 0
        output_files = [f for f in result.files if f.role == "output"]
        assert len(output_files) == 1
        assert Path(output_files[0].path).read_text().strip() == "hello world"

    def test_poll_tool_is_idempotent_on_completed_entry(
        self, tmp_path: Path, docker_available: bool
    ):
        if not docker_available:
            pytest.skip("Docker not available")

        spec = _make_echo_spec()
        session = LedgerSession(name="idempotent-test")
        session_dir = tmp_path / "sessions" / session.id

        entry, _ = run_tool(
            session=session, spec=spec, input_files={}, session_dir=session_dir, params={}
        )
        assert entry.run_status == "completed"
        files_before = list(entry.files)

        # Calling poll_tool() again on an already-finalized entry must be a no-op.
        result = poll_tool(entry, session_dir)
        assert result is entry
        assert entry.files == files_before


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
