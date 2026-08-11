"""Round-trip tests for crystallized Nextflow workflows and parameterized Snakefile generator.

Phase 2b-1: Harden Nextflow crystallization with round-trip tests against
the toolspec library, and validate the Snakefile fan-out generator
emits properly parameterized workflows (channels reference config[],
not hardcoded paths).
"""

from __future__ import annotations

from bioledger.forges.analysisforge.crystallize import (
    _make_nf_process,
    _proc_name,
    to_nextflow,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolParameter,
)


def _make_entry_from_spec(
    spec: ExecutionSpec,
    input_files: dict[str, str] | None = None,
    output_files: list[str] | None = None,
) -> LedgerEntry:
    """Create a LedgerEntry from an ExecutionSpec, simulating a run."""
    files: list[FileRef] = []
    for fname in output_files or []:
        files.append(FileRef(
            path=f"/tmp/{fname}", sha256="output_hash",
            size_bytes=100, role="output",
        ))
    for fname in (input_files or {}).values():
        files.append(FileRef(
            path=f"/tmp/{fname}", sha256="input_hash",
            size_bytes=100, role="input",
        ))
    return LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name=spec.name,
        container=ContainerInfo(
            image=spec.container,
            command=spec.command.split(),
            input_mapping=input_files or {},
        ),
        files=files,
        tool_spec_snapshot=spec.model_dump(),
    )


class TestNextflowProcessRoundTrip:
    """Round-trip: toolspec → NF process → verify key fields preserved."""

    def test_container_image_preserved(self):
        spec = ExecutionSpec(
            name="samtools_sort",
            container="quay.io/biocontainers/samtools:1.17--hdfd78af_0",
            command="samtools sort -o {{outputs.sorted.path}} {{inputs.bam}}",
            inputs={"bam": ToolInput(type=ParamType.FILE, format="bam", required=True)},
            outputs={"sorted": ToolOutput(format="bam", pattern="*.sorted.bam")},
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(spec, input_files={"bam": "reads.bam"})
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        assert "quay.io/biocontainers/samtools:1.17--hdfd78af_0" in nf_block

    def test_command_template_rendered(self):
        spec = ExecutionSpec(
            name="fastqc",
            container="quay.io/biocontainers/fastqc:0.12.1",
            command="fastqc {{inputs.reads}}",
            inputs={"reads": ToolInput(type=ParamType.FILE, format="fastq", required=True)},
            outputs={"report": ToolOutput(format="html", pattern="*_fastqc.html")},
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(spec, input_files={"reads": "sample.fastq"})
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        # Jinja renders {{inputs.reads}} → ${reads}
        assert "${reads}" in nf_block
        assert "fastqc" in nf_block

    def test_input_declarations_match_spec(self):
        spec = ExecutionSpec(
            name="bwa_mem",
            container="quay.io/biocontainers/bwa:0.7.17",
            command="bwa mem {{inputs.ref}} {{inputs.reads}}",
            inputs={
                "ref": ToolInput(type=ParamType.FILE, format="fasta", required=True),
                "reads": ToolInput(type=ParamType.FILE, format="fastq", required=True),
            },
            outputs={"bam": ToolOutput(format="bam", pattern="*.bam")},
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(
            spec,
            input_files={"ref": "ref.fa", "reads": "reads.fastq"},
        )
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        assert "path ref" in nf_block
        assert "path reads" in nf_block

    def test_output_declarations_match_spec(self):
        spec = ExecutionSpec(
            name="samtools_index",
            container="quay.io/biocontainers/samtools:1.17",
            command="samtools index {{inputs.bam}}",
            inputs={"bam": ToolInput(type=ParamType.FILE, format="bam", required=True)},
            outputs={"bai": ToolOutput(format="bai", pattern="*.bam.bai")},
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(
            spec,
            input_files={"bam": "sorted.bam"},
            output_files=["sorted.bam.bai"],
        )
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        # Should have output declaration with emit name
        assert "path" in nf_block
        assert "emit:" in nf_block

    def test_parameters_preserved_in_render(self):
        spec = ExecutionSpec(
            name="trimmomatic",
            container="quay.io/biocontainers/trimmomatic:0.39",
            command="trimmomatic SE -threads {{parameters.threads}} "
                    "{{inputs.reads}} {{outputs.trimmed}}",
            inputs={"reads": ToolInput(type=ParamType.FILE, format="fastq", required=True)},
            outputs={"trimmed": ToolOutput(format="fastq", pattern="*.trimmed.fastq")},
            parameters={
                "threads": ToolParameter(type=ParamType.INTEGER, default=4),
            },
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(spec, input_files={"reads": "reads.fastq"})
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        # Parameter default should be rendered
        assert "4" in nf_block

    def test_multi_output_emit_names(self):
        """Multi-output tools should get out0, out1, ... emit names."""
        spec = ExecutionSpec(
            name="multiqc",
            container="quay.io/biocontainers/multiqc:1.14",
            command="multiqc .",
            inputs={},
            outputs={
                "report": ToolOutput(format="html", pattern="multiqc_report.html"),
                "data": ToolOutput(format="txt", pattern="multiqc_data/*.txt"),
            },
            status=SpecStatus.VALID,
        )
        entry = _make_entry_from_spec(
            spec,
            output_files=["multiqc_report.html", "multiqc_data/sample1.txt"],
        )
        proc = _proc_name(entry, 0)
        nf_block = _make_nf_process(proc, entry)
        assert "emit: out0" in nf_block
        assert "emit: out1" in nf_block


class TestNextflowWorkflowWiring:
    """Test that the workflow block correctly wires multi-step chains."""

    def test_two_step_chain_sort_to_index(self):
        """samtools_sort → samtools_index chain via file-level matching."""
        session = LedgerSession(name="SortIndex")
        e1 = _make_entry_from_spec(
            ExecutionSpec(
                name="samtools_sort",
                container="quay.io/biocontainers/samtools:1.17",
                command="samtools sort -o sorted.bam input.bam",
                inputs={"bam": ToolInput(type=ParamType.FILE, format="bam", required=True)},
                outputs={"sorted": ToolOutput(format="bam", pattern="*.sorted.bam")},
                status=SpecStatus.VALID,
            ),
            input_files={"bam": "input.bam"},
            output_files=["input.sorted.bam"],
        )
        session.add(e1)
        e2 = _make_entry_from_spec(
            ExecutionSpec(
                name="samtools_index",
                container="quay.io/biocontainers/samtools:1.17",
                command="samtools index input.sorted.bam",
                inputs={"bam": ToolInput(type=ParamType.FILE, format="bam", required=True)},
                outputs={"bai": ToolOutput(format="bai", pattern="*.bam.bai")},
                status=SpecStatus.VALID,
            ),
            input_files={"bam": "input.sorted.bam"},
            output_files=["input.sorted.bam.bai"],
        )
        e2.parent_id = e1.id
        session.add(e2)

        result = to_nextflow(session)
        assert "step_0_samtools_sort" in result
        assert "step_1_samtools_index" in result
        # e2's input should be wired from e1's output
        assert "step_0_samtools_sort.out" in result


class TestSnakefileParameterization:
    """Test that the Snakefile fan-out generator emits parameterized workflows."""

    def _make_collection_spec(self) -> ExecutionSpec:
        return ExecutionSpec(
            name="samtools_sort",
            container="quay.io/biocontainers/samtools:1.17",
            command="samtools sort -o {{outputs.sorted.path}} {{inputs.bam}}",
            inputs={
                "bam": ToolInput(
                    type=ParamType.FILE, format="bam", required=True,
                    collection=True,
                ),
            },
            outputs={
                "sorted": ToolOutput(format="bam", pattern="*.sorted.bam"),
            },
            status=SpecStatus.VALID,
        )

    def test_snakefile_references_config_not_hardcoded(self):
        """Generated Snakefile should reference config[] not hardcoded paths."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        spec = self._make_collection_spec()
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam"},
        )
        content = handle.artifact_path.read_text()
        # Should reference config["bam_pattern"] not "data/*.bam"
        assert "config[" in content
        assert "data/*.bam" not in content
        # Should have configfile directive
        assert "configfile: 'config.yaml'" in content

    def test_snakefile_has_expand_for_fanout(self):
        """Generated Snakefile should use expand() for scatter."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        spec = self._make_collection_spec()
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam"},
        )
        content = handle.artifact_path.read_text()
        assert "expand(" in content
        assert "rule all:" in content

    def test_snakefile_container_directive(self):
        """Container image should be baked into the rule."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        spec = self._make_collection_spec()
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam"},
        )
        content = handle.artifact_path.read_text()
        assert 'container: "quay.io/biocontainers/samtools:1.17"' in content

    def test_snakefile_single_input_via_config(self):
        """Single (non-collection) inputs should be parameterized via config."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        spec = ExecutionSpec(
            name="bwa_mem",
            container="quay.io/biocontainers/bwa:0.7.17",
            command="bwa mem {{inputs.ref}} {{inputs.reads}}",
            inputs={
                "ref": ToolInput(
                    type=ParamType.FILE, format="fasta", required=True,
                    collection=False,
                ),
                "reads": ToolInput(
                    type=ParamType.FILE, format="fastq", required=True,
                    collection=True,
                ),
            },
            outputs={"bam": ToolOutput(format="bam", pattern="*.bam")},
            status=SpecStatus.VALID,
        )
        adapter = SnakemakeAdapter()
        handle = adapter.generate(
            spec=spec,
            input_mapping={"ref": "/data/ref.fa", "reads": "data/*.fastq"},
        )
        content = handle.artifact_path.read_text()
        # ref should be via config, not hardcoded
        assert 'config["ref"]' in content
        assert "/data/ref.fa" not in content

    def test_config_yaml_renames_collection_keys(self):
        """_build_config_yaml should rename collection inputs to <name>_pattern."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        yaml = adapter._build_config_yaml(
            input_mapping={"bam": "data/*.bam", "ref": "/data/ref.fa"},
            params={"threads": "4"},
            collection_inputs=["bam"],
        )
        assert "bam_pattern:" in yaml
        assert "data/*.bam" in yaml
        assert "ref:" in yaml
        assert "/data/ref.fa" in yaml
        assert "threads:" in yaml

    def test_workflow_handle_stores_collection_inputs(self):
        """WorkflowHandle.metadata should include collection_inputs list."""
        from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter

        adapter = SnakemakeAdapter()
        spec = self._make_collection_spec()
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam"},
        )
        assert "collection_inputs" in handle.metadata
        assert "bam" in handle.metadata["collection_inputs"]
