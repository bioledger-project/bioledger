"""Tests for ExecutionAdapter protocol, DockerRunnerFacade, and dispatch()."""

from __future__ import annotations

import pytest

from bioledger.core.engines.config import EnginesConfig
from bioledger.core.engines.dispatch import (
    _detect_installed_engine,
    dispatch,
    resolve_engine_for_fanout,
)
from bioledger.core.engines.docker_facade import DockerRunnerFacade
from bioledger.core.engines.protocol import (
    ExecutionAdapter,
    RunHandle,
    RunStatus,
    WorkflowHandle,
)


class TestProtocol:
    def test_docker_facade_is_execution_adapter(self):
        facade = DockerRunnerFacade()
        assert isinstance(facade, ExecutionAdapter)

    def test_run_status_defaults(self):
        rs = RunStatus(state="running")
        assert rs.state == "running"
        assert rs.completed == 0
        assert rs.total == 0
        assert rs.message == ""

    def test_workflow_handle_defaults(self):
        wh = WorkflowHandle(
            executor="snakemake",
            artifact_path="/tmp/Snakefile",
        )
        assert wh.executor == "snakemake"
        assert wh.step_inventory == []
        assert wh.container_image == ""
        assert wh.input_mapping == {}

    def test_run_handle_defaults(self):
        rh = RunHandle(
            executor="snakemake",
            run_id="job-1",
            entry_id="entry-abc",
            work_dir="/tmp/work",
        )
        assert rh.executor == "snakemake"
        assert rh.run_id == "job-1"
        assert rh.entry_id == "entry-abc"
        assert rh.status == "running"


class TestDockerFacade:
    def test_generate_raises_not_implemented(self):
        facade = DockerRunnerFacade()
        with pytest.raises(NotImplementedError, match="cannot fan-out"):
            facade.generate(spec=None, input_mapping={})  # type: ignore

    def test_submit_raises_not_implemented(self):
        facade = DockerRunnerFacade()
        wh = WorkflowHandle(executor="snakemake", artifact_path="/tmp/Snakefile")
        with pytest.raises(NotImplementedError, match="cannot submit"):
            facade.submit(wh, input_mapping={}, params={}, work_dir=None)  # type: ignore


class _MockSpec:
    """Minimal spec mock for dispatch tests."""

    def __init__(self, executors: list[str]) -> None:
        self.executors = executors


class TestDispatch:
    def test_docker_executor_returns_facade(self):
        spec = _MockSpec(executors=["docker"])
        adapter = dispatch(spec)
        assert isinstance(adapter, DockerRunnerFacade)

    def test_conda_executor_returns_facade(self):
        from bioledger.core.engines.conda_facade import CondaRunnerFacade
        spec = _MockSpec(executors=["conda"])
        adapter = dispatch(spec)
        assert isinstance(adapter, CondaRunnerFacade)

    def test_docker_and_conda_returns_facade(self):
        spec = _MockSpec(executors=["docker", "conda"])
        adapter = dispatch(spec)
        assert isinstance(adapter, DockerRunnerFacade)

    def test_snakemake_executor_returns_adapter(self):
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter
        spec = _MockSpec(executors=["snakemake"])
        adapter = dispatch(spec)
        assert isinstance(adapter, SnakemakeAdapter)

    def test_nextflow_executor_returns_adapter(self):
        from bioledger.core.engines.nextflow_adapter import NextflowAdapter
        spec = _MockSpec(executors=["nextflow"])
        adapter = dispatch(spec)
        assert isinstance(adapter, NextflowAdapter)

    def test_galaxy_executor_returns_adapter(self):
        from bioledger.core.engines.galaxy_adapter import GalaxyAdapter
        spec = _MockSpec(executors=["galaxy"])
        adapter = dispatch(spec)
        assert isinstance(adapter, GalaxyAdapter)

    def test_multiple_engine_executors_raises(self):
        spec = _MockSpec(executors=["nextflow", "galaxy"])
        with pytest.raises(ValueError, match="multiple engine executors"):
            dispatch(spec)

    def test_no_executors_raises(self):
        spec = _MockSpec(executors=[])
        with pytest.raises(ValueError, match="No usable executors"):
            dispatch(spec)

    def test_engine_override(self):
        from bioledger.core.engines.galaxy_adapter import GalaxyAdapter
        spec = _MockSpec(executors=["docker"])
        adapter = dispatch(spec, engine="galaxy")
        assert isinstance(adapter, GalaxyAdapter)


class TestResolveEngineForFanout:
    def test_explicit_engine(self):
        assert resolve_engine_for_fanout(engine="snakemake") == "snakemake"

    def test_config_default(self):
        cfg = EnginesConfig(default="galaxy")
        assert resolve_engine_for_fanout(config=cfg) == "galaxy"

    def test_auto_detect_returns_none_or_engine(self):
        result = _detect_installed_engine()
        # In test env, likely None — just verify it doesn't crash
        assert result is None or result in ("nextflow", "snakemake", "galaxy")

    def test_auto_no_engine_raises(self):
        cfg = EnginesConfig(default="auto")
        # If no engine is installed in test env, should raise
        detected = _detect_installed_engine()
        if detected is None:
            with pytest.raises(RuntimeError, match="Fan-out requires"):
                resolve_engine_for_fanout(config=cfg)
