"""Tests for engine config schema and LedgerSession.dependency_graph()."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bioledger.core.engines.config import (
    EnginesConfig,
    EngineConfig,
    GalaxyEngineConfig,
    NextflowEngineConfig,
    SnakemakeEngineConfig,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)


class TestEngineConfig:
    def test_default_engines_config(self):
        cfg = EnginesConfig()
        assert cfg.default == "auto"
        assert isinstance(cfg.snakemake, SnakemakeEngineConfig)
        assert isinstance(cfg.nextflow, NextflowEngineConfig)
        assert isinstance(cfg.galaxy, GalaxyEngineConfig)

    def test_explicit_default(self):
        cfg = EnginesConfig(default="snakemake")
        assert cfg.default == "snakemake"

    def test_invalid_default(self):
        with pytest.raises(ValidationError):
            EnginesConfig(default="docker")

    def test_base_engine_config_defaults(self):
        ec = EngineConfig()
        assert ec.executable is None
        assert ec.config_file is None
        assert ec.extra_args == []

    def test_galaxy_default_mode(self):
        gxy = GalaxyEngineConfig()
        assert gxy.mode == "planemo"

    def test_galaxy_remote_requires_url(self):
        with pytest.raises(ValidationError, match="url"):
            GalaxyEngineConfig(mode="remote", api_key="secret")

    def test_galaxy_remote_requires_api_key(self):
        with pytest.raises(ValidationError, match="api_key"):
            GalaxyEngineConfig(mode="remote", url="https://usegalaxy.org")

    def test_galaxy_remote_anonymous_ok(self):
        gxy = GalaxyEngineConfig(
            mode="remote", url="https://usegalaxy.org", api_key=""
        )
        assert gxy.api_key == ""

    def test_galaxy_remote_with_key(self):
        gxy = GalaxyEngineConfig(
            mode="remote",
            url="https://usegalaxy.org",
            api_key="abc123",
        )
        assert gxy.api_key == "abc123"

    def test_galaxy_planemo_no_url_needed(self):
        gxy = GalaxyEngineConfig(mode="planemo")
        assert gxy.url == ""
        assert gxy.api_key is None

    def test_extra_args_passthrough(self):
        ec = EngineConfig(extra_args=["--latency-wait", "60"])
        assert ec.extra_args == ["--latency-wait", "60"]


class TestDependencyGraph:
    def _make_entry(
        self,
        tool_name: str = "fastqc",
        input_mapping: dict[str, str] | None = None,
        output_files: list[str] | None = None,
        parent_id: str | None = None,
    ) -> LedgerEntry:
        files: list[FileRef] = []
        for fname in output_files or []:
            files.append(FileRef(
                path=f"/tmp/{fname}", sha256="output_hash",
                size_bytes=100, role="output",
            ))
        for fname in (input_mapping or {}).values():
            files.append(FileRef(
                path=f"/tmp/{fname}", sha256="input_hash",
                size_bytes=100, role="input",
            ))
        return LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name=tool_name,
            container=ContainerInfo(
                image="quay.io/biocontainers/test:1.0",
                command=["test"],
                input_mapping=input_mapping or {},
            ),
            parent_id=parent_id,
            exit_code=0,
            files=files,
        )

    def test_empty_session(self):
        session = LedgerSession(name="Empty")
        graph = session.dependency_graph()
        assert graph == {}

    def test_single_entry_no_deps(self):
        session = LedgerSession(name="Single")
        e = self._make_entry(output_files=["output.txt"])
        session.add(e)
        graph = session.dependency_graph()
        assert graph == {e.id: set()}

    def test_chained_entries(self):
        session = LedgerSession(name="Chain")
        e1 = self._make_entry(
            tool_name="producer",
            output_files=["data.txt"],
        )
        session.add(e1)
        e2 = self._make_entry(
            tool_name="consumer",
            input_mapping={"input": "data.txt"},
            output_files=["result.txt"],
            parent_id=e1.id,
        )
        session.add(e2)
        graph = session.dependency_graph()
        assert graph[e2.id] == {e1.id}
        assert graph[e1.id] == set()

    def test_multi_input_deps(self):
        """Entry consuming from two different producers has both as deps."""
        session = LedgerSession(name="Multi")
        e1 = self._make_entry(
            tool_name="msa",
            output_files=["gene1.aln"],
        )
        session.add(e1)
        e2 = self._make_entry(
            tool_name="tree",
            input_mapping={"alignment": "gene1.aln"},
            output_files=["gene1.treefile"],
            parent_id=e1.id,
        )
        session.add(e2)
        e3 = self._make_entry(
            tool_name="busted",
            input_mapping={"alignment": "gene1.aln", "tree": "gene1.treefile"},
            output_files=["gene1.json"],
            parent_id=e2.id,
        )
        session.add(e3)
        graph = session.dependency_graph()
        assert graph[e3.id] == {e1.id, e2.id}
