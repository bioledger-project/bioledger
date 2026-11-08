"""Tests for RO-Crate builder (unified crystallization).

Validates:
- All sessions (single/multi-engine) produce a single workflow.nf as mainEntity
- Engine-specific supplementary files (.smk, tool XML, nf-core modules) are included
- engine-env.yml is generated when engine entries are present
- nextflow.config uses profile-based container runtime selection
- ledger.json is always included
- Data files from entries are included
"""

from __future__ import annotations

import json

from bioledger.forges.crateforge.builder import (
    build_rocrate,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)


def _make_entry(
    tool_name: str = "fastqc",
    executor: str = "docker",
    output_files: list[str] | None = None,
    input_mapping: dict[str, str] | None = None,
    parent_id: str | None = None,
    spec_snapshot: dict | None = None,
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
            image="quay.io/biocontainers/fastqc:0.12.1",
            command=["fastqc"],
            input_mapping=input_mapping or {},
        ),
        executor=executor,
        files=files,
        parent_id=parent_id,
        tool_spec_snapshot=spec_snapshot,
        run_status="completed",
        exit_code=0,
    )


class TestBuildRocrateSingleWorkflow:
    """All sessions should produce a single workflow.nf as mainEntity."""

    def test_single_docker_entry(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry(
            tool_name="fastqc",
            executor="docker",
            output_files=["qc.html"],
        ))
        crate_dir = build_rocrate(session, tmp_path)
        assert crate_dir.exists()
        assert (crate_dir / "ledger.json").exists()
        assert (crate_dir / "workflow.nf").exists()

        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        root = next(e for e in graph if e.get("@id") == "./")
        main_ref = root["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        assert "workflow.nf" in main_id


class TestBuildRocrateMultiEngine:
    """Multi-engine sessions also produce a single workflow.nf (unified)."""

    def test_docker_plus_snakemake_single_workflow(self, tmp_path):
        session = LedgerSession(name="multi")
        e1 = _make_entry(
            tool_name="fastqc",
            executor="docker",
            output_files=["reads_fastqc.html"],
        )
        session.add(e1)
        e2 = _make_entry(
            tool_name="samtools_sort",
            executor="snakemake",
            output_files=["sorted.bam"],
            spec_snapshot={
                "name": "samtools_sort",
                "container": "quay.io/biocontainers/samtools:1.17",
                "command": "samtools sort -o sorted.bam input.bam",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        )
        session.add(e2)

        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "workflow.nf").exists()

        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        root = next(e for e in graph if e.get("@id") == "./")
        main_ref = root["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        assert "workflow.nf" in main_id

    def test_multi_engine_includes_engine_env_yml(self, tmp_path):
        session = LedgerSession(name="multi")
        session.add(_make_entry(executor="docker"))
        session.add(_make_entry(
            executor="snakemake",
            spec_snapshot={
                "name": "tool", "container": "img",
                "command": "echo hi",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))
        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "engine-env.yml").exists()

    def test_no_engine_env_yml_for_docker_only(self, tmp_path):
        session = LedgerSession(name="docker_only")
        session.add(_make_entry(executor="docker"))
        crate_dir = build_rocrate(session, tmp_path)
        assert not (crate_dir / "engine-env.yml").exists()

    def test_multi_engine_includes_ledger(self, tmp_path):
        """ledger.json should always be present."""
        session = LedgerSession(name="multi")
        session.add(_make_entry(executor="docker"))
        session.add(_make_entry(
            executor="snakemake",
            spec_snapshot={
                "name": "tool", "container": "img",
                "command": "echo hi",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))
        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "ledger.json").exists()

    def test_single_snakemake_produces_workflow_nf(self, tmp_path):
        """A single snakemake entry should produce workflow.nf (not composite)."""
        session = LedgerSession(name="snake_only")
        session.add(_make_entry(
            tool_name="samtools_sort", executor="snakemake",
            output_files=["sorted.bam"],
            spec_snapshot={
                "name": "samtools_sort",
                "container": "quay.io/biocontainers/samtools:1.17",
                "command": "samtools sort",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))
        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "workflow.nf").exists()

        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        root = next(e for e in graph if e.get("@id") == "./")
        main_ref = root["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        assert "workflow.nf" in main_id


class TestGalaxyNoPlaceholder:
    """Galaxy entries should NOT produce a markdown placeholder — they are
    included as Nextflow processes in the unified workflow.nf."""

    def test_galaxy_no_markdown_placeholder(self, tmp_path):
        session = LedgerSession(name="galaxy_test")
        session.add(_make_entry(
            tool_name="seqtk_galaxy", executor="galaxy",
            output_files=["output.fa"],
            spec_snapshot={
                "name": "seqtk_galaxy",
                "version": "1.0",
                "command": "planemo run tool.xml",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))

        crate_dir = build_rocrate(session, tmp_path)
        assert not (crate_dir / "galaxy_steps.md").exists()
        assert (crate_dir / "workflow.nf").exists()


class TestProfileBasedConfig:
    """K2: nextflow.config should use profile-based container runtime selection."""

    def test_docker_profile_enabled_for_docker_entries(self, tmp_path):
        session = LedgerSession(name="docker_only")
        session.add(_make_entry(
            tool_name="fastqc", executor="docker",
            output_files=["qc.html"],
        ))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "docker {" in config
        assert "docker.enabled = true" in config

    def test_conda_profile_enabled_for_conda_entries(self, tmp_path):
        session = LedgerSession(name="conda_config")
        session.add(_make_entry(
            tool_name="samtools_sort", executor="conda",
            output_files=["sorted.bam"],
            spec_snapshot={
                "name": "samtools_sort_conda",
                "conda_deps": ["samtools=1.17"],
                "command": "samtools sort",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "conda {" in config
        assert "conda.enabled = true" in config

    def test_conda_commented_when_only_docker(self, tmp_path):
        session = LedgerSession(name="docker_only_config")
        session.add(_make_entry(
            tool_name="fastqc", executor="docker",
            output_files=["qc.html"],
        ))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "// conda {" in config

    def test_singularity_always_available(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry(executor="docker"))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "singularity {" in config
        assert "singularity.enabled = true" in config

    def test_slurm_example_commented(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry(executor="docker"))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "// slurm {" in config

    def test_profiles_block_present(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry(executor="docker"))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "profiles {" in config

    def test_engine_env_yml_referenced_when_engine_present(self, tmp_path):
        session = LedgerSession(name="multi")
        session.add(_make_entry(executor="docker"))
        session.add(_make_entry(
            executor="snakemake",
            spec_snapshot={
                "name": "tool", "container": "img",
                "command": "echo hi",
                "inputs": {}, "outputs": {}, "status": "valid",
            },
        ))
        crate_dir = build_rocrate(session, tmp_path)
        config = (crate_dir / "nextflow.config").read_text()
        assert "engine-env.yml" in config


class TestGenerateNextflowConfig:
    def _cfg(self, entries):
        from bioledger.forges.crateforge.builder import (
            _generate_nextflow_config,
        )

        return _generate_nextflow_config(entries)

    def test_mixed_docker_conda_gets_standard_profile(self):
        cfg = self._cfg(
            [_make_entry(executor="docker"), _make_entry(executor="conda")]
        )
        assert "standard {" in cfg
        assert "docker.enabled = true" in cfg
        assert "conda.enabled = true" in cfg
        assert "// docker-only profile:" in cfg
        assert "// conda-only profile:" in cfg
        assert "-profile standard" in cfg

    def test_neither_runtime_uncomments_singularity(self):
        cfg = self._cfg([_make_entry(executor="snakemake")])
        assert "    singularity {" in cfg
        assert "// Engine runtimes: conda env create" in cfg

    def test_nextflow_manifest_version_pinned(self):
        e = _make_entry(executor="nextflow")
        e.executor_version = "24.10.0"
        cfg = self._cfg([e])
        assert "manifest {" in cfg
        assert "nextflowVersion = '>=24.10" in cfg

    def test_non_semver_version_not_pinned(self):
        e = _make_entry(executor="nextflow")
        e.executor_version = "unknown-build"
        cfg = self._cfg([e])
        assert "manifest {" not in cfg


class TestGenerateSnakefileFromEntry:
    def test_no_snapshot_returns_none(self):
        from bioledger.forges.crateforge.builder import (
            _generate_snakefile_from_entry,
        )

        assert _generate_snakefile_from_entry(
            _make_entry(spec_snapshot=None)
        ) is None

    def test_bad_snapshot_returns_none(self):
        from bioledger.forges.crateforge.builder import (
            _generate_snakefile_from_entry,
        )

        assert _generate_snakefile_from_entry(
            _make_entry(spec_snapshot={"bogus": True})
        ) is None

    def test_generate_failure_returns_none(self, monkeypatch):
        from bioledger.forges.crateforge import builder
        from bioledger.forges.crateforge.builder import (
            _generate_snakefile_from_entry,
        )
        from bioledger.toolspec.models import ExecutionSpec

        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_adapter._generate_snakefile",
            lambda *a: (_ for _ in ()).throw(RuntimeError("gen boom")),
        )
        entry = _make_entry(
            executor="snakemake",
            spec_snapshot=ExecutionSpec(name="t", command="c").model_dump(),
        )
        assert _generate_snakefile_from_entry(entry) is None


class TestEngineSupplementaryFiles:
    def _crate(self, monkeypatch):
        from bioledger.forges.crateforge import builder

        added = []
        crate = SimpleNamespace(
            add_file=lambda *a, **k: added.append((a, k))
        )
        return crate, added

    def test_companion_files_staged(self, tmp_path):
        from rocrate.rocrate import ROCrate

        from bioledger.forges.crateforge.builder import (
            _add_engine_supplementary_files,
        )

        src = tmp_path / "Snakefile"
        src.write_text("rule all:")
        e = _make_entry(executor="snakemake")
        e.companion_files = {"Snakefile": str(src), "gone": "/nope/x"}
        crate = ROCrate()
        _add_engine_supplementary_files(
            crate, tmp_path / "scratch", [e], None
        )
        scratch = tmp_path / "scratch"
        assert (scratch / e.id / "Snakefile").exists()
        # missing companion warns + skips, doesn't raise
        assert not (scratch / e.id / "gone").exists()

    def test_running_entry_skipped_unless_included(self, tmp_path):
        from rocrate.rocrate import ROCrate

        from bioledger.forges.crateforge.builder import (
            _add_engine_supplementary_files,
        )

        src = tmp_path / "S"
        src.write_text("x")
        e = _make_entry(executor="snakemake")
        e.companion_files = {"S": str(src)}
        e.run_status = "running"
        crate = ROCrate()
        scratch = tmp_path / "scratch"
        _add_engine_supplementary_files(crate, scratch, [e], None)
        assert not (scratch / e.id / "S").exists()
        _add_engine_supplementary_files(
            crate, scratch, [e], None, include_running=True
        )
        assert (scratch / e.id / "S").exists()


class TestBuildRocrateGaps:
    def test_entry_ids_filter(self, tmp_path):
        s = LedgerSession(name="s")
        e1, e2 = _make_entry(), _make_entry(tool_name="bwa")
        s.add(e1)
        s.add(e2)
        out = build_rocrate(s, tmp_path / "out", entry_ids=[e1.id])
        ledger = json.loads(
            (out / "ledger.json").read_text()
        )
        assert len(ledger["entries"]) == 1
        assert ledger["entries"][0]["id"] == e1.id

    def test_data_files_added_with_dedup(self, tmp_path):
        real = tmp_path / "out.bam"
        real.write_text("data")
        e = _make_entry()
        e.files.append(FileRef(
            path=str(real), sha256="h", size_bytes=4, role="output"
        ))
        s = LedgerSession(name="s")
        s.add(e)
        out = build_rocrate(s, tmp_path / "out")
        names = [p.name for p in out.rglob("*")]
        assert "out.bam" in names
        # file lands under <entry8>/ prefix
        assert (out / e.id[:8] / "out.bam").exists()

    def test_missing_data_file_skipped(self, tmp_path):
        e = _make_entry(output_files=["ghost.bam"])
        s = LedgerSession(name="s")
        s.add(e)
        out = build_rocrate(s, tmp_path / "out")
        assert not list(out.rglob("ghost.bam"))
