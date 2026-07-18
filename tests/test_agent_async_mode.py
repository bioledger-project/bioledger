"""Tests for AnalysisForgeAgent's async tool-execution mode dispatch.

Verifies run_tool_with_logging()'s mode resolution (explicit override vs.
the spec's suggested_mode) and poll_pending_jobs() finalization, using a
mocked DockerRunner so no real Docker daemon is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.config import BioLedgerConfig
from bioledger.core.containers.docker import RunResult
from bioledger.forges.analysisforge.executor import get_session_dir, submit_tool
from bioledger.ledger.models import LedgerSession
from bioledger.ledger.store import LedgerStore
from bioledger.toolspec.models import (
    ExecutionMode,
    ExecutionSpec,
    SpecStatus,
    ToolOutput,
    ToolSpec,
)


def _make_spec(name: str, suggested_mode: ExecutionMode) -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name=name,
            container="alpine:latest",
            command="echo hi > {{outputs.out.path}}",
            outputs={"out": ToolOutput(pattern="out.txt")},
            status=SpecStatus.VALID,
            suggested_mode=suggested_mode,
        )
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A real AnalysisForgeAgent, isolated to tmp_path.

    Constructing pydantic-ai Agents validates that *some* API key is
    present (but makes no network calls at construction time), and
    ToolStore()/ToolLibrary() fall back to ~/.bioledger unless redirected
    — so both are patched to stay inside tmp_path for test isolation.
    """
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-test-key")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from bioledger.forges.analysisforge.agent import AnalysisForgeAgent

    config = BioLedgerConfig(home_dir=tmp_path / ".bioledger")
    config.ensure_dirs()
    store = LedgerStore(db_path=tmp_path / "db.sqlite")
    session = LedgerSession(name="test")
    store.create_session(session)
    return AnalysisForgeAgent(config, session, store)


class TestModeResolution:
    async def test_explicit_async_mode_submits_and_returns_none_result(
        self, agent, tmp_path
    ):
        spec = _make_spec("blocking-tool", ExecutionMode.BLOCKING)
        agent.tool_store.save(spec)

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-123"

            entry, result = await agent.run_tool_with_logging(
                "blocking-tool",
                {},
                tmp_path / "session",
                mode=ExecutionMode.ASYNC,
            )

        assert result is None
        assert entry.run_status == "running"
        assert entry.container_id == "container-123"

    async def test_defers_to_spec_suggested_mode_when_none(self, agent, tmp_path):
        spec = _make_spec("async-tool", ExecutionMode.ASYNC)
        agent.tool_store.save(spec)

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-456"

            entry, result = await agent.run_tool_with_logging(
                "async-tool", {}, tmp_path / "session"
            )

        assert result is None
        assert entry.run_status == "running"

    async def test_explicit_blocking_overrides_async_suggested_mode(
        self, agent, tmp_path
    ):
        spec = _make_spec("async-tool-2", ExecutionMode.ASYNC)
        agent.tool_store.save(spec)

        mock_result = RunResult(exit_code=0, stdout="", stderr="", duration_seconds=0.1)
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-789"
            MockRunner.return_value.poll.return_value = mock_result

            entry, result = await agent.run_tool_with_logging(
                "async-tool-2",
                {},
                tmp_path / "session",
                mode=ExecutionMode.BLOCKING,
            )

        assert result is not None
        assert result.exit_code == 0
        assert entry.run_status == "completed"


class TestPollPendingJobs:
    def test_finalizes_running_jobs_when_container_completes(self, agent):
        spec = _make_spec("poll-tool", ExecutionMode.ASYNC)
        agent.tool_store.save(spec)

        session_dir = get_session_dir(agent.config.home_dir, agent.session.id)
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-poll"
            entry = submit_tool(agent.session, spec, {}, session_dir)

        assert entry.run_status == "running"

        mock_result = RunResult(exit_code=0, stdout="", stderr="", duration_seconds=1.0)
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.poll.return_value = mock_result
            completed = agent.poll_pending_jobs()

        assert len(completed) == 1
        assert completed[0].id == entry.id
        assert completed[0].run_status == "completed"

    def test_no_running_jobs_returns_empty(self, agent):
        assert agent.poll_pending_jobs() == []

    def test_still_running_job_not_returned(self, agent):
        spec = _make_spec("still-running-tool", ExecutionMode.ASYNC)
        agent.tool_store.save(spec)

        session_dir = get_session_dir(agent.config.home_dir, agent.session.id)
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-still-running"
            submit_tool(agent.session, spec, {}, session_dir)

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.poll.return_value = None  # still running
            completed = agent.poll_pending_jobs()

        assert completed == []
        assert agent.session.entries[0].run_status == "running"


class TestTimeoutSessionSave:
    """T3: run_tool_with_logging saves session on TimeoutError."""

    async def test_timeout_saves_session_with_failed_status(self, agent, tmp_path):
        spec = _make_spec("slow-tool", ExecutionMode.BLOCKING)
        agent.tool_store.save(spec)

        mock_result = None  # always "still running"
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-timeout"
            MockRunner.return_value.poll.return_value = mock_result
            MockRunner.return_value.client.containers.get.return_value = MagicMock()

            # Patch run_tool to use a near-zero timeout so the test
            # doesn't wait 3600s. The agent imports run_tool directly,
            # so we wrap it with a forced timeout.
            import bioledger.forges.analysisforge.agent as agent_mod

            original_run_tool = agent_mod.run_tool

            def quick_timeout_run_tool(*args, **kwargs):
                kwargs["timeout"] = 0
                kwargs["poll_interval"] = 0.001
                return original_run_tool(*args, **kwargs)

            with patch.object(agent_mod, "run_tool", side_effect=quick_timeout_run_tool):
                with pytest.raises(TimeoutError):
                    await agent.run_tool_with_logging(
                        "slow-tool",
                        {},
                        tmp_path / "session",
                        mode=ExecutionMode.BLOCKING,
                    )

        # Entry should be marked failed in the in-memory session
        entry = agent.session.entries[-1]
        assert entry.run_status == "failed"
        assert entry.exit_code == -1

        # Session should have been saved to disk (not just in-memory)
        loaded = agent.store.load_session(agent.session.id)
        assert loaded is not None
        loaded_entry = next(e for e in loaded.entries if e.id == entry.id)
        assert loaded_entry.run_status == "failed"
        assert loaded_entry.exit_code == -1


class TestPollPendingJobsDockerFailure:
    """T2/U2: poll_pending_jobs should not crash on Docker daemon errors."""

    def test_docker_exception_does_not_raise(self, agent):
        import docker.errors

        spec = _make_spec("docker-fail-tool", ExecutionMode.ASYNC)
        agent.tool_store.save(spec)

        session_dir = get_session_dir(agent.config.home_dir, agent.session.id)
        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.submit.return_value = "container-docker-fail"
            submit_tool(agent.session, spec, {}, session_dir)

        with patch("bioledger.forges.analysisforge.executor.DockerRunner") as MockRunner:
            MockRunner.return_value.poll.side_effect = docker.errors.DockerException(
                "Cannot connect to Docker daemon"
            )
            # Should raise DockerException (caller is expected to catch it)
            with pytest.raises(docker.errors.DockerException):
                agent.poll_pending_jobs()
