"""Tests for the per-run-directory executor model.

Verifies staging (symlinks for in-session, copies for external), collision-aware
naming, output discovery, sidecar colocation, and optional-input handling
without requiring Docker (unit tests use the internal helper functions).
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


class TestStageInputs:
    """Tests for _stage_inputs: per-run staging with symlinks and collision handling."""

    def test_external_files_are_copied(self, tmp_path: Path):
        """External files (outside session_dir) are copied into run_dir."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        run_dir = session_dir / "runs" / "run1"

        external = tmp_path / "external" / "ref.fasta"
        external.parent.mkdir()
        external.write_text(">chr1\nACGT\n")

        input_files = {"ref_fasta": external}
        mapping = _stage_inputs(input_files, session_dir, run_dir)

        assert mapping["ref_fasta"] == "ref.fasta"
        staged = run_dir / "ref.fasta"
        assert staged.exists()
        assert not staged.is_symlink()  # copied, not symlinked
        assert staged.read_text() == ">chr1\nACGT\n"

    def test_in_session_files_are_symlinked(self, tmp_path: Path):
        """Files inside session_dir (prior run outputs) are symlinked."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Simulate a prior run output
        prior_run = session_dir / "runs" / "prior"
        prior_run.mkdir(parents=True)
        prior_output = prior_run / "aligned.sam"
        prior_output.write_text("@HD\tVN:1.6\n")

        # New run that uses the prior output
        new_run = session_dir / "runs" / "new"
        input_files = {"input_sam": prior_output}
        mapping = _stage_inputs(input_files, session_dir, new_run)

        assert mapping["input_sam"] == "aligned.sam"
        staged = new_run / "aligned.sam"
        assert staged.is_symlink()
        assert staged.resolve() == prior_output.resolve()

    def test_basename_collision_gets_slot_prefix(self, tmp_path: Path):
        """When two inputs share a basename, they get slot-prefixed names."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        run_dir = session_dir / "runs" / "run1"

        # Two different files with same basename
        external_a = tmp_path / "source_a" / "genome.fasta"
        external_a.parent.mkdir()
        external_a.write_text(">chr1\nACGT\n")

        external_b = tmp_path / "source_b" / "genome.fasta"
        external_b.parent.mkdir()
        external_b.write_text(">chr2\nTGCATGCA\n")

        input_files = {"ref_a": external_a, "ref_b": external_b}
        mapping = _stage_inputs(input_files, session_dir, run_dir)

        # Both get prefixed because they collide
        assert mapping["ref_a"] == "ref_a__genome.fasta"
        assert mapping["ref_b"] == "ref_b__genome.fasta"

        # Both exist in run_dir
        assert (run_dir / "ref_a__genome.fasta").exists()
        assert (run_dir / "ref_b__genome.fasta").exists()

    def test_no_collision_preserves_basename(self, tmp_path: Path):
        """Non-colliding inputs keep their original basenames."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        run_dir = session_dir / "runs" / "run1"

        ref = tmp_path / "refs" / "genome.fasta"
        ref.parent.mkdir()
        ref.write_text(">chr1\nACGT\n")

        reads = tmp_path / "reads" / "sample_R1.fastq"
        reads.parent.mkdir()
        reads.write_text("@seq1\nACGT\n")

        input_files = {"ref_fasta": ref, "reads1": reads}
        mapping = _stage_inputs(input_files, session_dir, run_dir)

        assert mapping["ref_fasta"] == "genome.fasta"
        assert mapping["reads1"] == "sample_R1.fastq"

    def test_symlink_target_is_relative(self, tmp_path: Path):
        """In-session symlinks must use a relative target so they resolve
        inside the container (where the session tree is mounted at /sessions,
        not at the host path)."""
        import os

        session_dir = tmp_path / "session"
        session_dir.mkdir()

        prior_run = session_dir / "runs" / "prior"
        prior_run.mkdir(parents=True)
        prior_output = prior_run / "aligned.sam"
        prior_output.write_text("@HD\tVN:1.6\n")

        new_run = session_dir / "runs" / "new"
        _stage_inputs({"input_sam": prior_output}, session_dir, new_run)

        staged = new_run / "aligned.sam"
        link_target = os.readlink(staged)
        # Target must be relative, not an absolute host path
        assert not os.path.isabs(link_target)
        assert link_target == os.path.join("..", "prior", "aligned.sam")

    def test_external_directory_input_is_copied(self, tmp_path: Path):
        """Directory inputs (e.g. rtg-tools SDF template) are copied as a tree."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        run_dir = session_dir / "runs" / "run1"

        sdf = tmp_path / "external" / "reference.sdf"
        sdf.mkdir(parents=True)
        (sdf / "namedata0").write_bytes(b"\x01\x02")
        (sdf / "seqpointer0").write_bytes(b"\x03\x04")

        mapping = _stage_inputs({"template": sdf}, session_dir, run_dir)

        assert mapping["template"] == "reference.sdf"
        staged = run_dir / "reference.sdf"
        assert staged.is_dir()
        assert (staged / "namedata0").exists()
        assert (staged / "seqpointer0").exists()


class TestSnapshotDir:
    """Tests for _snapshot_dir: mtime tracking."""

    def test_captures_all_files_recursively(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("b")

        snap = _snapshot_dir(tmp_path)
        assert len(snap) == 2
        assert str((tmp_path / "a.txt").resolve()) in snap
        assert str((sub / "b.txt").resolve()) in snap

    def test_empty_dir_gives_empty_snapshot(self, tmp_path: Path):
        snap = _snapshot_dir(tmp_path)
        assert snap == {}


class TestDiscoverOutputs:
    """Tests for _discover_outputs: pattern-based + change detection."""

    def _make_spec(self, outputs: dict[str, str]) -> ToolSpec:
        """Helper to build a spec with given output patterns."""
        return ToolSpec(
            execution=ExecutionSpec(
                name="test",
                container="ubuntu:latest",
                command="true",
                outputs={
                    name: ToolOutput(format="any", pattern=pattern)
                    for name, pattern in outputs.items()
                },
                status=SpecStatus.VALID,
            )
        )

    def test_detects_new_file_matching_pattern(self, tmp_path: Path):
        spec = self._make_spec({"alignment": "aligned.sam"})
        pre = _snapshot_dir(tmp_path)
        staged = set()

        # Simulate tool writing output
        (tmp_path / "aligned.sam").write_text("@HD\tVN:1.6\n")

        found = _discover_outputs(spec, tmp_path, pre, staged)
        assert len(found) == 1
        assert found[0].name == "aligned.sam"

    def test_ignores_unchanged_files(self, tmp_path: Path):
        # Pre-existing file
        (tmp_path / "ref.fasta").write_text(">chr1\nACGT\n")
        spec = self._make_spec({"ref": "ref.fasta"})
        pre = _snapshot_dir(tmp_path)
        staged = set()

        # No change — nothing should be discovered
        found = _discover_outputs(spec, tmp_path, pre, staged)
        assert len(found) == 0

    def test_detects_sidecar_files_via_glob(self, tmp_path: Path):
        """bwa-mem2 index creates *.bwt.2bit.64 beside the FASTA."""
        spec = self._make_spec({"index": "*.bwt.2bit.64"})
        (tmp_path / "ref.fasta").write_text(">chr1\nACGT\n")
        pre = _snapshot_dir(tmp_path)
        staged = set()

        # Simulate bwa-mem2 index writing sidecar files
        (tmp_path / "ref.fasta.bwt.2bit.64").write_bytes(b"\x00" * 100)

        found = _discover_outputs(spec, tmp_path, pre, staged)
        assert len(found) == 1
        assert "bwt.2bit.64" in found[0].name

    def test_fallback_excludes_staged_inputs(self, tmp_path: Path):
        """When no patterns declared, fallback should not capture staged inputs."""
        spec = self._make_spec({})  # no declared patterns

        # Stage an input file
        (tmp_path / "input.bam").write_text("BAM content\n")
        pre = _snapshot_dir(tmp_path)
        staged = {str((tmp_path / "input.bam").resolve())}

        # New output file
        (tmp_path / "surprise_output.vcf").write_text("##fileformat=VCFv4.2\n")

        found = _discover_outputs(spec, tmp_path, pre, staged)
        assert len(found) == 1
        assert found[0].name == "surprise_output.vcf"

    def test_directory_pattern_captures_children(self, tmp_path: Path):
        """rtg-tools SDF pattern: *.sdf/ captures all files in the directory."""
        spec = self._make_spec({"sdf": "*.sdf/"})
        pre = _snapshot_dir(tmp_path)
        staged = set()

        sdf_dir = tmp_path / "reference.sdf"
        sdf_dir.mkdir()
        (sdf_dir / "namedata0").write_bytes(b"\x01\x02")
        (sdf_dir / "seqpointer0").write_bytes(b"\x03\x04")

        found = _discover_outputs(spec, tmp_path, pre, staged)
        assert len(found) == 2
        names = {p.name for p in found}
        assert names == {"namedata0", "seqpointer0"}


class TestRenderCommandOptionalInputs:
    """Tests for missing optional inputs rendering as empty strings."""

    def test_optional_absent_renders_empty(self):
        """An optional input not in input_mapping renders as empty string."""
        spec = ToolSpec(
            execution=ExecutionSpec(
                name="bwa-mem2-mem",
                container="quay.io/biocontainers/bwa-mem2:2.2.1",
                command=(
                    "bwa-mem2 mem -t {{parameters.threads}} "
                    "{{inputs.ref_fasta}} {{inputs.reads1}} {{inputs.reads2}}"
                ),
                inputs={
                    "ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True),
                    "reads1": ToolInput(type=ParamType.FILE, format="fastq", required=True),
                    "reads2": ToolInput(type=ParamType.FILE, format="fastq", required=False),
                },
                parameters={
                    "threads": ToolParameter(type=ParamType.INTEGER, default=4),
                },
                status=SpecStatus.VALID,
            )
        )

        # Single-end: no reads2
        input_mapping = {"ref_fasta": "ref.fasta", "reads1": "sample_R1.fastq.gz"}
        cmd = _render_command(spec, input_mapping, {})
        assert "/work/ref.fasta" in cmd
        assert "/work/sample_R1.fastq.gz" in cmd
        # reads2 should be empty, resulting in trailing space (acceptable)
        assert "reads2" not in cmd

    def test_all_inputs_present_renders_correctly(self):
        """When all inputs provided, all paths render correctly."""
        spec = ToolSpec(
            execution=ExecutionSpec(
                name="bwa-mem2-mem",
                container="quay.io/biocontainers/bwa-mem2:2.2.1",
                command=(
                    "bwa-mem2 mem {{inputs.ref_fasta}} "
                    "{{inputs.reads1}} {{inputs.reads2}} > {{outputs._dir}}/aligned.sam"
                ),
                inputs={
                    "ref_fasta": ToolInput(type=ParamType.FILE, format="fasta", required=True),
                    "reads1": ToolInput(type=ParamType.FILE, format="fastq", required=True),
                    "reads2": ToolInput(type=ParamType.FILE, format="fastq", required=False),
                },
                status=SpecStatus.VALID,
            )
        )

        input_mapping = {
            "ref_fasta": "ref.fasta",
            "reads1": "sample_R1.fastq.gz",
            "reads2": "sample_R2.fastq.gz",
        }
        cmd = _render_command(spec, input_mapping, {})
        assert "/work/ref.fasta" in cmd
        assert "/work/sample_R1.fastq.gz" in cmd
        assert "/work/sample_R2.fastq.gz" in cmd
        assert "> /work/aligned.sam" in cmd


class TestGetSessionDir:
    """Tests for get_session_dir and get_tool_run_dir helpers."""

    def test_get_session_dir_returns_path(self, tmp_path: Path):
        session = get_session_dir(tmp_path, "abc123")
        assert session == tmp_path / "sessions" / "abc123"

    def test_get_tool_run_dir_creates_nested(self, tmp_path: Path):
        run_dir = get_tool_run_dir(tmp_path, "abc123", "run_001")
        assert run_dir == tmp_path / "sessions" / "abc123" / "runs" / "run_001"
        assert run_dir.is_dir()

    def test_get_tool_run_dir_idempotent(self, tmp_path: Path):
        d1 = get_tool_run_dir(tmp_path, "x", "run1")
        d2 = get_tool_run_dir(tmp_path, "x", "run1")
        assert d1 == d2


class TestRenderCommandBioinformaticsSpecs:
    """Test rendering against actual library spec patterns."""

    def test_bwa_mem2_index_renders_correctly(self):
        """bwa-mem2-index: writes index files beside the FASTA (in-place)."""
        spec = ToolSpec(
            execution=ExecutionSpec(
                name="bwa-mem2-index",
                container="quay.io/biocontainers/bwa-mem2:2.2.1",
                command="bwa-mem2 index {{inputs.ref_fasta}}",
                inputs={
                    "ref_fasta": ToolInput(
                        type=ParamType.FILE, format="fasta", required=True
                    ),
                },
                outputs={
                    "index": ToolOutput(format="any", pattern="*.bwt.2bit.64"),
                },
                status=SpecStatus.VALID,
            )
        )
        input_mapping = {"ref_fasta": "reference.fasta"}
        cmd = _render_command(spec, input_mapping, {})
        assert cmd == "bwa-mem2 index /work/reference.fasta"

    def test_gatk_haplotypecaller_renders_correctly(self):
        """GATK HC: ref + bam inputs, sidecar inputs colocated."""
        spec = ToolSpec(
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
                outputs={
                    "gvcf": ToolOutput(format="vcf", pattern="output.g.vcf.gz"),
                },
                parameters={
                    "ploidy": ToolParameter(type=ParamType.INTEGER, default=2),
                },
                status=SpecStatus.VALID,
            )
        )
        input_mapping = {
            "input_bam": "sorted.bam",
            "ref_fasta": "reference.fasta",
            "ref_fai": "reference.fasta.fai",
            "ref_dict": "reference.dict",
        }
        cmd = _render_command(spec, input_mapping, {"ploidy": 1})
        assert "-R /work/reference.fasta" in cmd
        assert "-I /work/sorted.bam" in cmd
        assert "-O /work/output.g.vcf.gz" in cmd
        assert "-ploidy 1" in cmd

    def test_samtools_faidx_sidecar_output(self, tmp_path: Path):
        """samtools faidx writes .fai beside the input FASTA — verify discovery."""
        spec = ToolSpec(
            execution=ExecutionSpec(
                name="samtools-faidx",
                container="quay.io/biocontainers/samtools:1.19.2",
                command="samtools faidx {{inputs.ref_fasta}}",
                inputs={
                    "ref_fasta": ToolInput(
                        type=ParamType.FILE, format="fasta", required=True
                    ),
                },
                outputs={
                    "fai": ToolOutput(format="fai", pattern="*.fai"),
                },
                status=SpecStatus.VALID,
            )
        )

        # Simulate work dir state before tool run
        work = tmp_path / "work"
        work.mkdir()
        (work / "reference.fasta").write_text(">chr1\nACGT\n")
        pre = _snapshot_dir(work)
        staged = {str((work / "reference.fasta").resolve())}

        # Simulate samtools faidx output (writes .fai beside the FASTA)
        (work / "reference.fasta.fai").write_text("chr1\t4\t6\t4\t5\n")

        found = _discover_outputs(spec, work, pre, staged)
        assert len(found) == 1
        assert found[0].name == "reference.fasta.fai"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
