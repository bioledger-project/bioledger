"""Integration smoke test: variant-calling pipeline command rendering.

Validates that the entire variant-calling pipeline (faidx → bwa-index →
bwa-mem → sort → bam-index → haplotypecaller) renders correct commands
using the per-run-directory model, proving that sidecar colocation
and chained execution work end-to-end.

This test does NOT require Docker — it verifies the executor logic only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bioledger.forges.analysisforge.executor import (
    _discover_outputs,
    _render_command,
    _snapshot_dir,
    _stage_inputs,
    get_session_dir,
    get_tool_run_dir,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolParameter,
    ToolSpec,
)


# --- Tool spec fixtures mimicking the library ---


def _samtools_faidx_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="samtools-faidx",
            container="quay.io/biocontainers/samtools:1.20",
            command="samtools faidx {{inputs.ref_fasta}}",
            inputs={"ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True)},
            outputs={"fai": ToolOutput(format="any", pattern="*.fai")},
            status=SpecStatus.VALID,
        )
    )


def _samtools_dict_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="samtools-dict",
            container="quay.io/biocontainers/samtools:1.20",
            command="samtools dict {{inputs.ref_fasta}} > {{outputs._dir}}/reference.dict",
            inputs={"ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True)},
            outputs={"dict": ToolOutput(format="dict", pattern="*.dict")},
            status=SpecStatus.VALID,
        )
    )


def _bwa_index_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="bwa-mem2-index",
            container="quay.io/biocontainers/bwa-mem2:2.2.1",
            command="bwa-mem2 index {{inputs.ref_fasta}}",
            inputs={"ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True)},
            outputs={"index": ToolOutput(format="any", pattern="*.bwt.2bit.64")},
            status=SpecStatus.VALID,
        )
    )


def _bwa_mem_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="bwa-mem2-mem",
            container="quay.io/biocontainers/bwa-mem2:2.2.1",
            command=(
                "bwa-mem2 mem -t {{parameters.threads}} "
                "{{inputs.ref_fasta}} {{inputs.reads1}} {{inputs.reads2}} "
                "> {{outputs._dir}}/aligned.sam"
            ),
            inputs={
                "ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True),
                "reads1": ToolInput(type=ParamType.FILE, format="fastq", required=True),
                "reads2": ToolInput(type=ParamType.FILE, format="fastq", required=False),
            },
            outputs={"alignment": ToolOutput(format="sam", pattern="aligned.sam")},
            parameters={"threads": ToolParameter(type=ParamType.INTEGER, default=4)},
            status=SpecStatus.VALID,
        )
    )


def _samtools_sort_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="samtools-sort",
            container="quay.io/biocontainers/samtools:1.20",
            command=(
                "samtools sort -@ {{parameters.threads}} -o {{outputs._dir}}/sorted.bam "
                "{{inputs.input_sam}}"
            ),
            inputs={"input_sam": ToolInput(type=ParamType.FILE, format="sam", required=True)},
            outputs={"sorted_bam": ToolOutput(format="bam", pattern="sorted.bam")},
            parameters={"threads": ToolParameter(type=ParamType.INTEGER, default=4)},
            status=SpecStatus.VALID,
        )
    )


def _samtools_index_bam_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="samtools-index",
            container="quay.io/biocontainers/samtools:1.20",
            command="samtools index {{inputs.input_bam}}",
            inputs={"input_bam": ToolInput(type=ParamType.FILE, format="bam", required=True)},
            outputs={"bam_index": ToolOutput(format="any", pattern="*.bai")},
            status=SpecStatus.VALID,
        )
    )


def _gatk_hc_spec() -> ToolSpec:
    return ToolSpec(
        execution=ExecutionSpec(
            name="gatk-haplotypecaller",
            container="broadinstitute/gatk:4.5.0.0",
            command=(
                "gatk HaplotypeCaller "
                "-R {{inputs.ref_fasta}} -I {{inputs.input_bam}} "
                "-O {{outputs._dir}}/output.g.vcf.gz "
                "-ploidy {{parameters.ploidy}} -ERC GVCF"
            ),
            inputs={
                "input_bam": ToolInput(type=ParamType.FILE, format="bam", required=True),
                "ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True),
                "ref_fai": ToolInput(type=ParamType.FILE, format="fai", required=True),
                "ref_dict": ToolInput(type=ParamType.FILE, format="dict", required=True),
            },
            outputs={"gvcf": ToolOutput(format="vcf", pattern="output.g.vcf.gz")},
            parameters={"ploidy": ToolParameter(type=ParamType.INTEGER, default=2)},
            status=SpecStatus.VALID,
        )
    )


class TestVariantCallingPipeline:
    """End-to-end command rendering for the variant calling pipeline."""

    def test_step1_samtools_faidx(self):
        """Step 1: samtools faidx writes .fai beside the reference."""
        cmd = _render_command(
            _samtools_faidx_spec(),
            {"ref_fasta": "reference.fasta"},
            {},
        )
        assert cmd == "samtools faidx /work/reference.fasta"

    def test_step2_samtools_dict(self):
        """Step 2: samtools dict writes .dict."""
        cmd = _render_command(
            _samtools_dict_spec(),
            {"ref_fasta": "reference.fasta"},
            {},
        )
        assert cmd == "samtools dict /work/reference.fasta > /work/reference.dict"

    def test_step3_bwa_index(self):
        """Step 3: bwa-mem2 index creates FM-index files beside the FASTA."""
        cmd = _render_command(
            _bwa_index_spec(),
            {"ref_fasta": "reference.fasta"},
            {},
        )
        assert cmd == "bwa-mem2 index /work/reference.fasta"

    def test_step4_bwa_mem_paired(self):
        """Step 4: bwa-mem2 mem aligns paired-end reads."""
        cmd = _render_command(
            _bwa_mem_spec(),
            {"ref_fasta": "reference.fasta", "reads1": "R1.fastq.gz", "reads2": "R2.fastq.gz"},
            {"threads": 8},
        )
        assert "bwa-mem2 mem -t 8" in cmd
        assert "/work/reference.fasta" in cmd
        assert "/work/R1.fastq.gz" in cmd
        assert "/work/R2.fastq.gz" in cmd
        assert "> /work/aligned.sam" in cmd

    def test_step5_samtools_sort(self):
        """Step 5: samtools sort produces sorted.bam."""
        cmd = _render_command(
            _samtools_sort_spec(),
            {"input_sam": "aligned.sam"},
            {"threads": 8},
        )
        assert "samtools sort -@ 8" in cmd
        assert "-o /work/sorted.bam" in cmd
        assert "/work/aligned.sam" in cmd

    def test_step6_samtools_index_bam(self):
        """Step 6: samtools index produces .bai beside the BAM."""
        cmd = _render_command(
            _samtools_index_bam_spec(),
            {"input_bam": "sorted.bam"},
            {},
        )
        assert cmd == "samtools index /work/sorted.bam"

    def test_step7_gatk_haplotypecaller(self):
        """Step 7: GATK HC uses all sidecar files colocated in /work."""
        cmd = _render_command(
            _gatk_hc_spec(),
            {
                "input_bam": "sorted.bam",
                "ref_fasta": "reference.fasta",
                "ref_fai": "reference.fasta.fai",
                "ref_dict": "reference.dict",
            },
            {"ploidy": 1},
        )
        assert "-R /work/reference.fasta" in cmd
        assert "-I /work/sorted.bam" in cmd
        assert "-O /work/output.g.vcf.gz" in cmd
        assert "-ploidy 1" in cmd
        assert "-ERC GVCF" in cmd

    def test_full_pipeline_staging_and_discovery(self, tmp_path: Path):
        """Integration: stage inputs in per-run dirs, simulate tool outputs, discover results."""
        session_dir = get_session_dir(tmp_path, "test-session")
        session_dir.mkdir(parents=True)

        # External reference file
        ref_dir = tmp_path / "refs"
        ref_dir.mkdir()
        ref_file = ref_dir / "reference.fasta"
        ref_file.write_text(">chr1\nACGTACGT\n")

        # Run 1: faidx - stages external ref, outputs .fai
        run1_dir = get_tool_run_dir(tmp_path, "test-session", "run1")
        input_files = {"ref_fasta": ref_file}
        mapping1 = _stage_inputs(input_files, session_dir, run1_dir)
        assert mapping1["ref_fasta"] == "reference.fasta"
        assert (run1_dir / "reference.fasta").exists()  # copied

        # Simulate faidx output: sidecar .fai written beside the staged ref
        pre = _snapshot_dir(run1_dir)
        (run1_dir / "reference.fasta.fai").write_text("chr1\t8\t6\t8\t9\n")
        staged1 = {str((run1_dir / "reference.fasta").resolve())}
        found = _discover_outputs(_samtools_faidx_spec(), run1_dir, pre, staged1)
        assert len(found) == 1
        assert found[0].name == "reference.fasta.fai"

        # Run 2: dict - stages ref (symlink from run1 output) and produces .dict
        ref_in_run1 = run1_dir / "reference.fasta"
        run2_dir = get_tool_run_dir(tmp_path, "test-session", "run2")
        mapping2 = _stage_inputs({"ref_fasta": ref_in_run1}, session_dir, run2_dir)
        assert mapping2["ref_fasta"] == "reference.fasta"
        assert (run2_dir / "reference.fasta").is_symlink()

        pre = _snapshot_dir(run2_dir)
        (run2_dir / "reference.dict").write_text("@HD\tVN:1.6\n@SQ\tSN:chr1\tLN:8\n")
        staged2 = {str((run2_dir / "reference.fasta").resolve())}
        found = _discover_outputs(_samtools_dict_spec(), run2_dir, pre, staged2)
        assert len(found) == 1
        assert found[0].name == "reference.dict"

        # Run 3: bwa-mem2 index - stages ref and produces index sidecars
        run3_dir = get_tool_run_dir(tmp_path, "test-session", "run3")
        mapping3 = _stage_inputs({"ref_fasta": ref_in_run1}, session_dir, run3_dir)
        assert mapping3["ref_fasta"] == "reference.fasta"
        pre = _snapshot_dir(run3_dir)
        (run3_dir / "reference.fasta.bwt.2bit.64").write_bytes(b"\x00" * 200)
        (run3_dir / "reference.fasta.pac").write_bytes(b"\x00" * 50)
        staged3 = {str((run3_dir / "reference.fasta").resolve())}
        found = _discover_outputs(_bwa_index_spec(), run3_dir, pre, staged3)
        assert any("bwt.2bit.64" in f.name for f in found)

        # Verify colocation: ref + .fai + .dict + index sidecars all in their run dirs
        assert (run1_dir / "reference.fasta").exists()
        assert (run1_dir / "reference.fasta.fai").exists()
        assert (run2_dir / "reference.dict").exists()
        assert (run3_dir / "reference.fasta.bwt.2bit.64").exists()

    def test_reference_bundle_is_colocated_in_run_dir(self, tmp_path: Path):
        """The reference, its sidecars, and dict output all share the run directory.

        This is the invariant GATK depends on: ref.fasta, ref.fasta.fai, and
        the .dict must sit in the same directory. Per-run dirs ensure this.
        """
        session_dir = get_session_dir(tmp_path, "coloc-test")
        session_dir.mkdir(parents=True)
        run_dir = get_tool_run_dir(tmp_path, "coloc-test", "run1")

        ref_dir = tmp_path / "GCF_000002765.6"
        ref_dir.mkdir()
        ref_file = ref_dir / "reference.fasta"
        ref_file.write_text(">chr1\nACGTACGT\n")

        # Stage external ref into run dir
        mapping = _stage_inputs({"ref_fasta": ref_file}, session_dir, run_dir)
        staged_ref = run_dir / mapping["ref_fasta"]

        # Sidecars written beside the staged reference (as real tools do)
        fai = run_dir / "reference.fasta.fai"
        fai.write_text("chr1\t8\t6\t8\t9\n")
        # dict written to outputs._dir == /work (the run dir)
        dict_file = run_dir / "reference.dict"
        dict_file.write_text("@HD\tVN:1.6\n")

        # All three live in the SAME directory (the run dir)
        assert staged_ref.parent == fai.parent == dict_file.parent == run_dir

    def test_multiple_references_in_isolated_dirs_no_collision(self, tmp_path: Path):
        """Two same-named references in separate run dirs don't interfere."""
        session_dir = get_session_dir(tmp_path, "multiref-test")
        session_dir.mkdir(parents=True)

        # Host reference
        host_dir = tmp_path / "GCF_HOST"
        host_dir.mkdir()
        (host_dir / "genome.fasta").write_text(">host\nACGT\n")

        # Pathogen reference (same filename!)
        path_dir = tmp_path / "GCF_PATHOGEN"
        path_dir.mkdir()
        (path_dir / "genome.fasta").write_text(">pathogen\nTTTT\n")

        # Separate run dirs - both can use the same basename
        run1_dir = get_tool_run_dir(tmp_path, "multiref-test", "run1")
        mapping1 = _stage_inputs({"ref": host_dir / "genome.fasta"}, session_dir, run1_dir)
        assert mapping1["ref"] == "genome.fasta"
        assert (run1_dir / "genome.fasta").read_text() == ">host\nACGT\n"

        run2_dir = get_tool_run_dir(tmp_path, "multiref-test", "run2")
        mapping2 = _stage_inputs({"ref": path_dir / "genome.fasta"}, session_dir, run2_dir)
        assert mapping2["ref"] == "genome.fasta"
        assert (run2_dir / "genome.fasta").read_text() == ">pathogen\nTTTT\n"

    def test_chained_input_is_symlinked(self, tmp_path: Path):
        """When an input resolves to a prior run output, it's symlinked."""
        session_dir = get_session_dir(tmp_path, "chain-test")
        session_dir.mkdir(parents=True)

        # First run produces aligned.sam
        run1_dir = get_tool_run_dir(tmp_path, "chain-test", "run1")
        (run1_dir / "aligned.sam").write_text("@HD\tVN:1.6\n")

        # Second run stages the prior output (symlink because it's in session dir)
        run2_dir = get_tool_run_dir(tmp_path, "chain-test", "run2")
        prior_output = run1_dir / "aligned.sam"
        mapping = _stage_inputs({"input_sam": prior_output}, session_dir, run2_dir)

        assert mapping["input_sam"] == "aligned.sam"
        staged = run2_dir / "aligned.sam"
        assert staged.is_symlink()
        assert staged.resolve() == prior_output.resolve()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
