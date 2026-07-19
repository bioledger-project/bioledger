from __future__ import annotations

from bioledger.forges.analysisforge.crystallize import (
    to_galaxy_workflow,
    to_nextflow,
    to_nextflow_from_entries,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)


def _make_entry(
    kind: EntryKind = EntryKind.TOOL_RUN,
    tool_name: str = "fastqc",
    image: str = "quay.io/biocontainers/fastqc:0.11.9--0",
    command: list[str] | None = None,
    parent_id: str | None = None,
    input_mapping: dict[str, str] | None = None,
    output_files: list[str] | None = None,
    input_sha: str = "input_hash",
    output_sha: str = "output_hash",
) -> LedgerEntry:
    files: list[FileRef] = []
    for fname in output_files or []:
        files.append(FileRef(
            path=f"/tmp/{fname}", sha256=output_sha,
            size_bytes=100, role="output",
        ))
    for fname in (input_mapping or {}).values():
        files.append(FileRef(
            path=f"/tmp/{fname}", sha256=input_sha,
            size_bytes=100, role="input",
        ))
    return LedgerEntry(
        kind=kind,
        tool_spec_name=tool_name,
        container=ContainerInfo(
            image=image,
            command=command or ["fastqc", "reads.fastq"],
            input_mapping=input_mapping or {},
        ),
        parent_id=parent_id,
        exit_code=0,
        files=files,
    )


def test_to_nextflow_empty_session():
    session = LedgerSession(name="Empty")
    result = to_nextflow(session)
    assert "Empty workflow" in result


def test_to_nextflow_single_entry():
    session = LedgerSession(name="Single")
    entry = _make_entry()
    session.add(entry)
    result = to_nextflow(session)
    assert "process step_0_fastqc" in result
    assert "workflow {" in result
    assert "container" in result


def test_to_nextflow_chained_entries():
    """Three entries chained via file-level input/output matching."""
    session = LedgerSession(name="Chain")
    e1 = _make_entry(
        tool_name="fastqc",
        output_files=["reads_fastqc.html"],
    )
    session.add(e1)
    e2 = _make_entry(
        tool_name="trimmomatic",
        parent_id=e1.id,
        input_mapping={"input_fastq": "reads.fastq"},
        output_files=["trimmed.fastq"],
    )
    session.add(e2)
    e3 = _make_entry(
        tool_name="hisat2",
        parent_id=e2.id,
        input_mapping={"input_fastq": "trimmed.fastq"},
        output_files=["aligned.bam"],
    )
    session.add(e3)

    result = to_nextflow(session)
    assert "step_0_fastqc" in result
    assert "step_1_trimmomatic" in result
    assert "step_2_hisat2" in result
    # e2 consumes trimmed.fastq from e1 — but e1 doesn't produce it.
    # e2's input reads.fastq is a root input (not produced by e1).
    # e3 consumes trimmed.fastq which e2 produces — should chain.
    assert "step_2_hisat2" in result


def test_to_nextflow_file_level_chaining():
    """Verify that inputs are wired to the correct prior process output
    by filename matching, not just parent_id."""
    session = LedgerSession(name="FileChain")
    e1 = _make_entry(
        tool_name="producer",
        output_files=["reference.fa", "reference.fa.fai"],
    )
    session.add(e1)
    e2 = _make_entry(
        tool_name="consumer",
        parent_id=e1.id,
        input_mapping={"ref": "reference.fa", "index": "reference.fa.fai"},
        output_files=["result.txt"],
    )
    session.add(e2)

    result = to_nextflow(session)
    # Both inputs should be wired to e1's outputs via emit names
    assert "step_0_producer" in result
    assert "step_1_consumer" in result
    assert "step_0_producer.out.out0" in result  # reference.fa
    assert "step_0_producer.out.out1" in result  # reference.fa.fai
    # Should NOT have root channel for these inputs
    assert "Channel.fromPath" not in result or "params.input" in result


def test_to_nextflow_self_reference_avoided():
    """When an entry's input and output share the same basename (e.g. bgzip
    compresses .fna.gz to .fna.gz), the input must NOT be wired to the
    entry's own output — it should be a root input from the crate.
    But the output SHOULD be available for downstream consumers."""
    session = LedgerSession(name="SelfRef")
    e1 = _make_entry(
        tool_name="bgzip",
        input_mapping={"input_gz": "reference.fna.gz"},
        output_files=["reference.fna.gz", "reference.fna.gz.gzi"],
        input_sha="raw_gzip_hash",   # different from output
        output_sha="bgzip_hash",     # legitimate same-name output
    )
    session.add(e1)
    # Downstream consumer of the bgzip-compressed .fna.gz
    e2 = _make_entry(
        tool_name="samtools_faidx",
        parent_id=e1.id,
        input_mapping={"ref": "reference.fna.gz"},
        output_files=["reference.fna.gz.fai"],
    )
    session.add(e2)

    result = to_nextflow(session)
    assert "step_0_bgzip" in result
    assert "step_1_samtools_faidx" in result
    # bgzip's own input should be a crate channel, NOT a self-reference
    wf_section = result.split("workflow {")[1]
    bgzip_call = wf_section.split("step_0_bgzip(")[1].split(")")[0]
    assert "step_0_bgzip.out" not in bgzip_call
    assert "Channel.fromPath" in bgzip_call
    # Downstream faidx should get bgzip's output, not a crate channel
    faidx_call = wf_section.split("step_1_samtools_faidx(")[1].split(")")[0]
    assert "step_0_bgzip.out.out0" in faidx_call


def test_to_nextflow_staged_input_excluded_from_outputs():
    """When a staged input leaks into the output list (same file, same sha256
    as input and output of an entry), it should not be used as a source for
    other entries' inputs — the real producer should be used instead."""
    session = LedgerSession(name="Leak")
    # e1 produces reference.fa
    e1 = _make_entry(tool_name="download", output_files=["reference.fa"])
    session.add(e1)
    # e2 has reference.fa as BOTH input and output with SAME sha256 (staged leak)
    e2 = _make_entry(
        tool_name="samtools_dict",
        parent_id=e1.id,
        input_mapping={"ref": "reference.fa"},
        output_files=["reference.fa", "reference.dict"],  # reference.fa is leaked
        input_sha="same_hash",
        output_sha="same_hash",  # same sha256 = staged input, not real output
    )
    session.add(e2)
    # e3 consumes reference.fa — should wire to e1 (the real producer), not e2
    e3 = _make_entry(
        tool_name="consumer",
        parent_id=e2.id,
        input_mapping={"ref": "reference.fa"},
        output_files=["result.txt"],
    )
    session.add(e3)

    result = to_nextflow(session)
    # e3's input should come from e1 (download), not e2 (samtools-dict)
    assert "step_0_download.out.out0" in result
    # e2's reference.fa should NOT appear as a source
    assert "step_1_samtools_dict.out.out0" not in result


def test_to_nextflow_skips_non_tool_entries():
    session = LedgerSession(name="Mixed")
    session.add(LedgerEntry(kind=EntryKind.DATA_IMPORT))
    session.add(_make_entry(tool_name="fastqc"))
    result = to_nextflow(session)
    # Only tool_run should produce a process
    assert result.count("process ") == 1


def test_to_nextflow_include_running():
    """With include_running=True, running entries should appear in the workflow."""
    session = LedgerSession(name="Running")
    e1 = _make_entry(
        tool_name="producer",
        output_files=["data.txt"],
    )
    session.add(e1)
    e2 = _make_entry(
        tool_name="consumer",
        parent_id=e1.id,
        input_mapping={"input": "data.txt"},
        output_files=["result.txt"],
    )
    e2.run_status = "running"
    session.add(e2)

    # Without include_running: only e1 appears
    result = to_nextflow(session)
    assert "step_0_producer" in result
    assert "step_1_consumer" not in result

    # With include_running: both appear
    result = to_nextflow(session, include_running=True)
    assert "step_0_producer" in result
    assert "step_1_consumer" in result


def test_to_nextflow_from_entries_subset():
    e1 = _make_entry(tool_name="fastqc", output_files=["output.html"])
    e2 = _make_entry(
        tool_name="hisat2",
        parent_id=e1.id,
        input_mapping={"input": "output.html"},
    )
    result = to_nextflow_from_entries([e1, e2])
    assert "step_0_fastqc" in result
    assert "step_1_hisat2" in result


def test_to_nextflow_from_entries_empty():
    result = to_nextflow_from_entries([])
    assert "Empty workflow" in result


def test_to_nextflow_from_entries_independent_roots():
    """Multiple independent roots represent parallel branches — valid, not a warning."""
    e1 = _make_entry(tool_name="fastqc", output_files=["a.html"])
    e2 = _make_entry(tool_name="hisat2", output_files=["b.bam"])  # no shared files
    result = to_nextflow_from_entries([e1, e2])
    assert "process step_0_fastqc" in result
    assert "process step_1_hisat2" in result
    # Neither process should chain to the other
    assert "step_0_fastqc.out" not in result
    assert "step_1_hisat2.out" not in result
    # No bogus warning about parallel independent runs
    assert "WARNING" not in result


def test_to_galaxy_workflow_empty():
    session = LedgerSession(name="Empty")
    result = to_galaxy_workflow(session)
    assert result["a_galaxy_workflow"] == "true"
    assert result["steps"] == {}


def test_to_galaxy_workflow_chained():
    session = LedgerSession(name="Galaxy")
    e1 = _make_entry(tool_name="fastqc")
    session.add(e1)
    e2 = _make_entry(tool_name="hisat2", parent_id=e1.id)
    session.add(e2)

    result = to_galaxy_workflow(session)
    assert len(result["steps"]) == 2
    step_1 = result["steps"]["1"]
    assert step_1["tool_id"] == "hisat2"
    assert step_1["input_connections"]["input"]["id"] == 0
