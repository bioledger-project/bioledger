"""Regression tests for the second codebase review pass.

Covers:
- ``| stem`` / ``| stem(true)`` / ``| basename`` filters on Nextflow
  ``${var}`` placeholders emitting Path properties (crystallize + fan-out
  generator) instead of no-ops on the literal text.
- Omitted optional inputs not producing ``path`` declarations,
  ``${var}`` placeholders, or ``Channel.fromPath(params.input)``.
- Most-recent-producer wiring for same-basename rewrite chains.
- ``engine-env.yml`` emitting ``planemo=`` for galaxy entries.
- ``nextflow.config`` with a container runtime staged for fan-out runs.
- ``_verify_and_record(skip_sha256=True)`` for the SRA recompression path.
"""

from __future__ import annotations

import pytest

from bioledger.forges.analysisforge.crystallize import (
    _render_script_for_nextflow,
    generate_engine_env_yml,
    to_nextflow_from_entries,
)
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
)


def _entry(
    *,
    name: str = "tool",
    command: str = "",
    spec_inputs: dict | None = None,
    spec_outputs: dict | None = None,
    input_mapping: dict[str, str] | None = None,
    output_files: list[tuple[str, str]] | None = None,
    input_files: list[tuple[str, str]] | None = None,
    executor: str = "docker",
    executor_version: str = "",
) -> LedgerEntry:
    """Build a ledger entry.

    ``output_files``/``input_files`` are (basename, sha256) pairs so the
    staged-input leak filter and producer map can be exercised.
    """
    files: list[FileRef] = []
    for fname, sha in input_files or []:
        files.append(
            FileRef(path=f"/tmp/{fname}", sha256=sha, size_bytes=1, role="input")
        )
    for fname, sha in output_files or []:
        files.append(
            FileRef(path=f"/tmp/{fname}", sha256=sha, size_bytes=1, role="output")
        )
    return LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name=name,
        tool_spec_snapshot={
            "name": name,
            "command": command,
            "inputs": spec_inputs or {},
            "outputs": spec_outputs or {},
            "parameters": {},
        },
        container=ContainerInfo(
            image="img:1",
            command=[],
            input_mapping=input_mapping or {},
        ),
        executor=executor,
        executor_version=executor_version,
        run_status="completed",
        files=files,
    )


class TestNextflowStemFilters:
    def test_stem_strips_last_extension(self):
        """{{ inputs.x | stem }} -> ${x.baseName} in crystallized scripts."""
        entry = _entry(
            command="tool -i {{ inputs.bam }} -x {{ inputs.bam | stem }}.idx",
            spec_inputs={"bam": {"required": True}},
            input_mapping={"bam": "reads.bam"},
        )
        script = _render_script_for_nextflow(entry, ["bam"])
        assert "${bam.baseName}.idx" in script
        assert "samtools" not in script  # sanity: template rendered as given

    def test_stem_true_strips_all_extensions(self):
        """{{ inputs.x | stem(true) }} -> ${x.simpleName} (samtools-dict style)."""
        entry = _entry(
            command=(
                "samtools dict -o "
                "{{outputs._dir}}/{{inputs.ref_fasta|stem(true)}}.dict "
                "{{inputs.ref_fasta}}"
            ),
            spec_inputs={"ref_fasta": {"required": True}},
            input_mapping={"ref_fasta": "ref.fna.gz"},
        )
        script = _render_script_for_nextflow(entry, ["ref_fasta"])
        assert "./${ref_fasta.simpleName}.dict" in script
        # Bare input still renders the path variable
        assert " ${ref_fasta}" in script

    def test_basename_maps_to_name_property(self):
        entry = _entry(
            command="tool {{ inputs.fq | basename }}",
            spec_inputs={"fq": {"required": True}},
            input_mapping={"fq": "r1.fq"},
        )
        script = _render_script_for_nextflow(entry, ["fq"])
        assert "${fq.name}" in script

    def test_fanout_main_nf_stem_filter(self, tmp_path):
        """The fan-out main.nf generator has the same placeholder problem."""
        from bioledger.core.engines.nextflow_adapter import (
            _generate_nextflow_workflow,
        )

        class _I:
            def __init__(self, collection=False):
                self.collection = collection
                self.required = True

        class _O:
            def __init__(self, pattern):
                self.pattern = pattern

        class _Spec:
            name = "bgzip"
            inputs = {"input_gz": _I()}
            outputs = {"out": _O("*.out")}
            parameters = {}
            command = "gunzip -k -f {{inputs.input_gz}} && bgzip -i -f {{inputs.input_gz|stem}}"

        _generate_nextflow_workflow(
            _Spec(), {"input_gz": "data/x.fna.gz"}, "img:1", tmp_path / "main.nf"
        )
        nf = (tmp_path / "main.nf").read_text()
        assert "${input_gz.baseName}" in nf
        # The literal broken render would run bgzip on the .gz itself
        assert "bgzip -i -f ${input_gz}\n" not in nf


class TestOptionalInputs:
    def test_omitted_optional_not_declared_or_wired(self):
        """An optional input absent from input_mapping must produce no
        path declaration, no ${var} render, and no params.input channel."""
        entry = _entry(
            name="bwa_mem",
            command=(
                "bwa mem {{ inputs.ref }} {{ inputs.r1 }} {{ inputs.r2 }} "
                "{% if inputs.r2 %}-p{% endif %}"
            ),
            spec_inputs={
                "ref": {"required": True},
                "r1": {"required": True},
                "r2": {"required": False},
            },
            input_mapping={"ref": "ref.fa", "r1": "reads.fq"},
            output_files=[("aln.bam", "sha_out")],
        )
        result = to_nextflow_from_entries([entry])
        assert "path r2" not in result
        assert "${r2}" not in result
        assert "Channel.fromPath(params.input)" not in result
        assert "-p" not in result  # {% if inputs.r2 %} must be false
        # Both real inputs still declared and wired to the crate
        assert "path ref" in result
        assert "path r1" in result

    def test_mapped_inputs_use_spec_order(self):
        entry = _entry(
            command="t {{ inputs.b }} {{ inputs.a }}",
            spec_inputs={"a": {}, "b": {}},
            input_mapping={"b": "b.f", "a": "a.f"},  # insertion order ≠ spec
        )
        result = to_nextflow_from_entries([entry])
        # Declarations follow spec order (a then b)
        a_idx = result.index("path a")
        b_idx = result.index("path b")
        assert a_idx < b_idx


class TestMostRecentProducer:
    def test_in_place_rewrite_wires_latest_producer(self):
        """e1 produces x.bam; e2 rewrites it (new sha); e3 must consume
        e2's output, not e1's."""
        e1 = _entry(
            name="producer",
            command="make x.bam",
            output_files=[("x.bam", "sha_v1")],
        )
        e2 = _entry(
            name="rewriter",
            command="rewrite {{ inputs.bam }}",
            spec_inputs={"bam": {}},
            input_mapping={"bam": "x.bam"},
            input_files=[("x.bam", "sha_v1")],
            output_files=[("x.bam", "sha_v2")],  # different sha → produced
        )
        e3 = _entry(
            name="consumer",
            command="use {{ inputs.bam }}",
            spec_inputs={"bam": {}},
            input_mapping={"bam": "x.bam"},
            input_files=[("x.bam", "sha_v2")],
        )
        result = to_nextflow_from_entries([e1, e2, e3])
        # e3 (step_2) must take e2's output channel
        workflow = result[result.index("workflow {"):]
        assert "step_1_rewriter.out.out0" in workflow
        # ...and NOT the original producer
        consumer_line = [
            ln for ln in workflow.splitlines() if "step_2_consumer(" in ln
        ][0]
        assert "step_0_producer" not in consumer_line

    def test_rewriter_input_wires_original_producer(self):
        """e2's own input still resolves to e1 (the file it consumed)."""
        e1 = _entry(name="producer", command="make x.bam",
                    output_files=[("x.bam", "sha_v1")])
        e2 = _entry(
            name="rewriter",
            command="rewrite {{ inputs.bam }}",
            spec_inputs={"bam": {}},
            input_mapping={"bam": "x.bam"},
            input_files=[("x.bam", "sha_v1")],
            output_files=[("x.bam", "sha_v2")],
        )
        result = to_nextflow_from_entries([e1, e2])
        rewriter_call = [
            ln for ln in result.splitlines() if "step_1_rewriter(" in ln
        ][0]
        assert "step_0_producer.out.out0" in rewriter_call


class TestEngineEnvYml:
    def test_galaxy_emits_planemo_package(self):
        entry = _entry(
            name="ts_tool",
            command="planemo run tool.xml job.yml",
            executor="galaxy",
            executor_version="galaxy 0.75.1",
            output_files=[("out.txt", "s")],
        )
        yml = generate_engine_env_yml([entry])
        assert "planemo=0.75.1" in yml
        assert "- galaxy=" not in yml


class TestFanoutNextflowConfig:
    def test_submit_writes_container_config(self, tmp_path):
        """adapter.submit must stage nextflow.config enabling a runtime so
        `container '<img>'` directives in main.nf aren't inert."""
        from bioledger.core.engines.config import NextflowEngineConfig
        from bioledger.core.engines.nextflow_adapter import NextflowAdapter
        from bioledger.core.engines.protocol import WorkflowHandle

        work = tmp_path / "fanout"
        artifact = tmp_path / "gen.nf"
        artifact.write_text("process p { container 'img:1' script: 'true' }\n")

        handle = WorkflowHandle(
            executor="nextflow",
            artifact_path=artifact,
            step_inventory=["p"],
            container_image="img:1",
            input_mapping={},
            output_patterns=["*.out"],
            metadata={"collection_inputs": []},
        )
        adapter = NextflowAdapter(
            NextflowEngineConfig(executable="/bin/true")
        )
        adapter.submit(handle, {}, {}, work)

        cfg = work / "nextflow.config"
        assert cfg.exists()
        content = cfg.read_text()
        assert "enabled = true" in content
        assert "docker" in content or "singularity" in content

    def test_user_config_file_suppresses_generated_config(self, tmp_path):
        from bioledger.core.engines.config import NextflowEngineConfig
        from bioledger.core.engines.nextflow_adapter import NextflowAdapter
        from bioledger.core.engines.protocol import WorkflowHandle

        user_cfg = tmp_path / "user.config"
        user_cfg.write_text("singularity.enabled = true\n")
        artifact = tmp_path / "gen.nf"
        artifact.write_text("process p { script: 'true' }\n")

        handle = WorkflowHandle(
            executor="nextflow",
            artifact_path=artifact,
            step_inventory=["p"],
            container_image="img:1",
            input_mapping={},
            output_patterns=[],
            metadata={},
        )
        adapter = NextflowAdapter(
            NextflowEngineConfig(
                executable="/bin/true", config_file=user_cfg
            )
        )
        adapter.submit(handle, {}, {}, tmp_path / "w")
        assert not (tmp_path / "w" / "nextflow.config").exists()


class TestVerifyAndRecordSRA:
    def test_skip_sha256_records_on_disk_digest(self, tmp_path):
        """SRA path: manifest sha covers the remote archive; the recorded
        digest must match the gzipped file actually written to disk."""
        from bioledger_isatab_schema.dataset import DataFile
        from bioledger_isatab_schema.download import (
            _hash_file,
            _verify_and_record,
        )

        local = tmp_path / "SRR1_1.fastq.gz"
        local.write_bytes(b"\x1f\x8bgzipped-bytes")
        sha, md5, size = _hash_file(local)

        f = DataFile(
            location="https://x/SRR1_1.fastq.gz",
            is_remote=True,
            sha256="manifest_archive_sha",
            md5="manifest_archive_md5",
        )
        _verify_and_record(
            f, local, sha, md5, size, skip_md5=True, skip_sha256=True
        )
        # Recorded digest == on-disk digest → resume/cache check passes
        assert f.sha256 == sha
        assert f.downloaded_path == str(local)

    def test_skip_sha256_still_records_digest(self, tmp_path):
        from bioledger_isatab_schema.dataset import DataFile
        from bioledger_isatab_schema.download import (
            _verify_and_record,
        )

        local = tmp_path / "f.fastq.gz"
        local.write_bytes(b"data")
        f = DataFile(location="https://x/f.fastq.gz", is_remote=True)
        _verify_and_record(
            f, local, "newsha", "newmd5", 4,
            skip_md5=True, skip_sha256=True,
        )
        assert f.sha256 == "newsha"
        assert f.md5 == "newmd5"
        assert f.size_bytes == 4

    def test_sha_mismatch_still_raises_without_skip(self, tmp_path):
        from bioledger_isatab_schema.dataset import DataFile
        from bioledger_isatab_schema.download import _verify_and_record

        local = tmp_path / "f.gz"
        local.write_bytes(b"data")
        f = DataFile(
            location="https://x/f.gz", is_remote=True, sha256="expected"
        )
        with pytest.raises(ValueError, match="sha256 mismatch"):
            _verify_and_record(f, local, "got", "m", 4)
        assert not local.exists()


class TestMostRecentProducerDeps:
    def test_dependency_graph_uses_latest_producer(self):
        from bioledger.forges.analysisforge.crystallize import (
            _build_dependency_graph,
        )

        e1 = _entry(name="a", output_files=[("x.bam", "s1")])
        e2 = _entry(
            name="b",
            input_mapping={"bam": "x.bam"},
            input_files=[("x.bam", "s1")],
            output_files=[("x.bam", "s2")],
        )
        e3 = _entry(
            name="c",
            input_mapping={"bam": "x.bam"},
            input_files=[("x.bam", "s2")],
        )
        deps = _build_dependency_graph([e1, e2, e3])
        assert deps[e3.id] == {e2.id}
        assert deps[e2.id] == {e1.id}
