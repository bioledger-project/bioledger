"""Tests for multi-engine RO-Crate builder (Phase 4).

Validates:
- Single-engine sessions (docker/conda) keep the existing Nextflow mainEntity path
- Multi-engine sessions produce a composite mainEntity with hasPart references
- Engine artifacts (workflow.nf, *.smk, workflow.ga) are added as supplementary files
- ledger.json is always included
- Data files from entries are included
"""

from __future__ import annotations

import json

from bioledger.forges.crateforge.builder import (
    _build_composite_entity,
    _detect_executors,
    _is_single_engine,
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


class TestDetectExecutors:
    def test_single_docker(self):
        entries = [_make_entry(executor="docker")]
        assert _detect_executors(entries) == {"docker"}

    def test_mixed_docker_snakemake(self):
        entries = [
            _make_entry(tool_name="fastqc", executor="docker"),
            _make_entry(tool_name="samtools_sort", executor="snakemake"),
        ]
        assert _detect_executors(entries) == {"docker", "snakemake"}

    def test_skips_non_tool_entries(self):
        entries = [
            _make_entry(executor="docker"),
            LedgerEntry(kind=EntryKind.DATA_IMPORT),
        ]
        assert _detect_executors(entries) == {"docker"}


class TestIsSingleEngine:
    def test_docker_only(self):
        assert _is_single_engine({"docker"}) is True

    def test_conda_only(self):
        assert _is_single_engine({"conda"}) is True

    def test_docker_conda(self):
        assert _is_single_engine({"docker", "conda"}) is True

    def test_docker_snakemake(self):
        assert _is_single_engine({"docker", "snakemake"}) is False

    def test_empty(self):
        assert _is_single_engine(set()) is True


class TestBuildRocrateSingleEngine:
    """Single-engine sessions should use the existing Nextflow mainEntity path."""

    def test_single_docker_entry(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry(
            tool_name="fastqc",
            output_files=["reads_fastqc.html"],
        ))
        crate_dir = build_rocrate(session, tmp_path)
        assert crate_dir.exists()
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        main_entities = [
            e for e in graph
            if "mainEntity" in e and e.get("mainEntity")
        ]
        assert len(main_entities) >= 1
        main_ref = main_entities[0]["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        assert "workflow.nf" in main_id

    def test_includes_ledger_json(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry())
        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "ledger.json").exists()

    def test_includes_nextflow_config(self, tmp_path):
        session = LedgerSession(name="test")
        session.add(_make_entry())
        crate_dir = build_rocrate(session, tmp_path)
        assert (crate_dir / "nextflow.config").exists()

    def test_empty_session(self, tmp_path):
        session = LedgerSession(name="empty")
        crate_dir = build_rocrate(session, tmp_path)
        assert crate_dir.exists()
        assert (crate_dir / "ledger.json").exists()


class TestBuildRocrateMultiEngine:
    """Multi-engine sessions should produce a composite mainEntity."""

    def test_docker_plus_snakemake_composite(self, tmp_path):
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
                "inputs": {},
                "outputs": {},
                "status": "valid",
            },
        )
        session.add(e2)

        crate_dir = build_rocrate(session, tmp_path)
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]

        # Find the root entity's mainEntity
        root = next(e for e in graph if e.get("@id") == "./")
        main_ref = root["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        assert "composite-workflow" in main_id

        # The composite entity should have hasPart
        composite = next(
            e for e in graph if e.get("@id") == "composite-workflow"
        )
        assert "hasPart" in composite
        part_ids = [
            p["@id"] if isinstance(p, dict) else p
            for p in composite["hasPart"]
        ]
        # Should have at least the docker (workflow.nf) and snakemake artifacts
        assert len(part_ids) >= 2

    def test_composite_has_dependency_info(self, tmp_path):
        session = LedgerSession(name="deps")
        e1 = _make_entry(
            tool_name="fastqc",
            executor="docker",
            output_files=["output.html"],
        )
        session.add(e1)
        e2 = _make_entry(
            tool_name="hisat2",
            executor="snakemake",
            input_mapping={"input": "output.html"},
            parent_id=e1.id,
            output_files=["aligned.bam"],
            spec_snapshot={
                "name": "hisat2",
                "container": "quay.io/biocontainers/hisat2:2.2.1",
                "command": "hisat2 -x ref -f input",
                "inputs": {},
                "outputs": {},
                "status": "valid",
            },
        )
        session.add(e2)

        crate_dir = build_rocrate(session, tmp_path)
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        composite = next(
            e for e in graph if e.get("@id") == "composite-workflow"
        )
        desc = composite.get("description", "")
        assert "fastqc" in desc
        assert "hisat2" in desc
        assert "docker" in desc
        assert "snakemake" in desc

    def test_three_engines_composite(self, tmp_path):
        """docker + snakemake + nextflow entries → composite with 3 parts."""
        session = LedgerSession(name="triple")
        session.add(_make_entry(
            tool_name="fastqc", executor="docker",
            output_files=["qc.html"],
        ))
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
        session.add(_make_entry(
            tool_name="nf_pipeline", executor="nextflow",
            output_files=["results.txt"],
        ))

        crate_dir = build_rocrate(session, tmp_path)
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        composite = next(
            e for e in graph if e.get("@id") == "composite-workflow"
        )
        part_ids = [
            p["@id"] if isinstance(p, dict) else p
            for p in composite["hasPart"]
        ]
        assert len(part_ids) >= 3

    def test_composite_includes_ledger(self, tmp_path):
        """ledger.json should always be present, even in multi-engine crates."""
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

    def test_single_snakemake_not_composite(self, tmp_path):
        """A single snakemake entry (no docker) should still get composite
        since it's not docker/conda single-engine."""
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
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        graph = metadata["@graph"]
        root = next(e for e in graph if e.get("@id") == "./")
        main_ref = root["mainEntity"]
        main_id = main_ref["@id"] if isinstance(main_ref, dict) else main_ref
        # Single snakemake → not _is_single_engine → composite path
        assert "composite-workflow" in main_id


class TestCompositeEntity:
    """Unit tests for _build_composite_entity."""

    def test_has_part_references(self, tmp_path):
        from rocrate.rocrate import ROCrate

        crate = ROCrate()
        session = LedgerSession(name="test")
        entries = [_make_entry(executor="docker"), _make_entry(executor="snakemake")]
        for e in entries:
            session.add(e)

        composite = _build_composite_entity(
            crate, ["workflow.nf", "samtools_sort.smk"], session, entries
        )
        assert composite.id == "composite-workflow"
        has_part = composite["hasPart"]
        assert len(has_part) == 2
        part_ids = [p["@id"] if isinstance(p, dict) else p for p in has_part]
        assert "workflow.nf" in part_ids
        assert "samtools_sort.smk" in part_ids

    def test_description_includes_step_info(self, tmp_path):
        from rocrate.rocrate import ROCrate

        crate = ROCrate()
        session = LedgerSession(name="test")
        e1 = _make_entry(tool_name="fastqc", executor="docker")
        session.add(e1)
        e2 = _make_entry(
            tool_name="hisat2", executor="snakemake",
            parent_id=e1.id,
        )
        session.add(e2)

        composite = _build_composite_entity(
            crate, ["workflow.nf", "hisat2.smk"], session, [e1, e2]
        )
        desc = composite["description"]
        assert "fastqc" in desc
        assert "hisat2" in desc
        assert "docker" in desc
        assert "snakemake" in desc
