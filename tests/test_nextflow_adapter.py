"""Tests for the Nextflow adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bioledger.core.engines.dispatch import _adapter_registry, dispatch
from bioledger.core.engines.nextflow_adapter import (
    NextflowAdapter,
    _parse_collection_value,
    _resolve_container,
)


class _MockInput:
    def __init__(self, name="", collection=False, format="any"):
        self.name = name
        self.collection = collection
        self.format = format


class _MockOutput:
    def __init__(self, name="", pattern="*.out", format="any"):
        self.name = name
        self.pattern = pattern
        self.format = format


class _MockSpec:
    def __init__(
        self,
        name="samtools_sort",
        container="quay.io/biocontainers/samtools:1.17",
        conda_deps=None,
        command="samtools sort -o {{ outputs.sorted_bam }} {{ inputs.bam }}",
        inputs=None,
        outputs=None,
        executors=None,
    ):
        self.name = name
        self.container = container
        self.conda_deps = conda_deps or []
        self.command = command
        self.inputs = inputs or {"bam": _MockInput("bam", collection=True)}
        self.outputs = outputs or {
            "sorted_bam": _MockOutput("sorted_bam", "*.sorted.bam")
        }
        self.executors = executors or ["nextflow"]


class TestParseCollectionValue:
    def test_glob(self):
        kind, pattern = _parse_collection_value("data/*.bam")
        assert kind == "glob"
        assert pattern == "data/*.bam"

    def test_manifest(self):
        kind, pattern = _parse_collection_value("data/manifest.tsv")
        assert kind == "manifest"

    def test_entry_id(self):
        kind, pattern = _parse_collection_value("abc123def456")
        assert kind == "entry_id"


class TestResolveContainer:
    def test_explicit_container(self):
        spec = _MockSpec(container="quay.io/biocontainers/samtools:1.17")
        assert _resolve_container(spec) == "quay.io/biocontainers/samtools:1.17"

    def test_conda_deps(self):
        spec = _MockSpec(container="", conda_deps=["samtools=1.17"])
        with patch(
            "bioledger.core.engines.nextflow_adapter.derive_container_image",
            return_value="quay.io/biocontainers/samtools:1.17--hdfd78af_0",
        ):
            assert _resolve_container(spec) == "quay.io/biocontainers/samtools:1.17--hdfd78af_0"

    def test_no_container_no_deps(self):
        spec = _MockSpec(container="", conda_deps=[])
        assert _resolve_container(spec) == ""


class TestNextflowGenerate:
    def test_generate_produces_nextflow_workflow(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        content = handle.artifact_path.read_text()
        assert "process samtools_sort" in content
        assert "workflow {" in content
        assert handle.executor == "nextflow"
        assert "samtools_sort" in handle.step_inventory

    def test_generate_has_container_directive(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec(container="quay.io/biocontainers/samtools:1.17")
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        content = handle.artifact_path.read_text()
        assert "container 'quay.io/biocontainers/samtools:1.17'" in content

    def test_generate_parameterized_via_params(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        content = handle.artifact_path.read_text()
        assert "params.bam_pattern" in content

    def test_generate_has_publish_dir(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        content = handle.artifact_path.read_text()
        assert "publishDir" in content

    def test_generate_single_input_via_params(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec(
            inputs={
                "ref": _MockInput("ref", collection=False),
                "reads": _MockInput("reads", collection=True),
            },
        )
        handle = adapter.generate(
            spec=spec,
            input_mapping={"ref": "/data/ref.fa", "reads": "data/*.fastq"},
        )
        content = handle.artifact_path.read_text()
        assert "params.ref" in content
        assert "params.reads_pattern" in content

    def test_generate_stores_collection_inputs(self, tmp_path):
        adapter = NextflowAdapter()
        spec = _MockSpec()
        handle = adapter.generate(spec=spec, input_mapping={"bam": "data/*.bam"})
        assert "collection_inputs" in handle.metadata
        assert "bam" in handle.metadata["collection_inputs"]


class TestNextflowParamsJson:
    def test_params_json_renames_collection_keys(self):
        adapter = NextflowAdapter()
        result = adapter._build_params_json(
            input_mapping={"bam": "data/*.bam", "ref": "/data/ref.fa"},
            params={"threads": "4"},
            collection_inputs=["bam"],
        )
        data = json.loads(result)
        assert "bam_pattern" in data
        assert data["bam_pattern"] == "data/*.bam"
        assert "ref" in data
        assert data["ref"] == "/data/ref.fa"
        assert "threads" in data

    def test_params_json_no_collection_inputs(self):
        adapter = NextflowAdapter()
        result = adapter._build_params_json(
            input_mapping={"input": "/data/file.bam"},
            params={},
        )
        data = json.loads(result)
        assert "input" in data
        assert data["input"] == "/data/file.bam"


class TestNextflowPoll:
    def test_poll_invalid_pid(self):
        adapter = NextflowAdapter()
        from bioledger.core.engines.protocol import RunHandle
        handle = RunHandle(
            executor="nextflow",
            run_id="not-a-pid",
            entry_id="",
            work_dir=Path("/tmp"),
        )
        assert adapter.poll(handle) is None

    def test_poll_completed_process(self, tmp_path):
        adapter = NextflowAdapter()
        from bioledger.core.engines.protocol import RunHandle
        log_path = tmp_path / "nextflow.log"
        log_path.write_text("Workflow completed successfully\nCompleted process foo\n")
        handle = RunHandle(
            executor="nextflow",
            run_id="999999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "completed"

    def test_poll_failed_process(self, tmp_path):
        adapter = NextflowAdapter()
        from bioledger.core.engines.protocol import RunHandle
        log_path = tmp_path / "nextflow.log"
        log_path.write_text("Error: something went wrong\n")
        handle = RunHandle(
            executor="nextflow",
            run_id="999999999",
            entry_id="",
            work_dir=tmp_path,
        )
        result = adapter.poll(handle)
        assert result is not None
        assert result.state == "failed"


class TestNextflowCollect:
    def test_collect_from_results_dir(self, tmp_path):
        adapter = NextflowAdapter()
        from bioledger.core.engines.protocol import RunHandle
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "output.txt").write_text("result")
        (results_dir / "sub").mkdir()
        (results_dir / "sub" / "data.bam").write_text("bam")
        handle = RunHandle(
            executor="nextflow",
            run_id="123",
            entry_id="",
            work_dir=tmp_path,
        )
        outputs = adapter.collect(handle)
        assert len(outputs) == 2
        names = {o["name"] for o in outputs}
        assert "output.txt" in names
        assert "data.bam" in names

    def test_collect_empty_work_dir(self, tmp_path):
        adapter = NextflowAdapter()
        from bioledger.core.engines.protocol import RunHandle
        handle = RunHandle(
            executor="nextflow",
            run_id="123",
            entry_id="",
            work_dir=tmp_path,
        )
        outputs = adapter.collect(handle)
        assert outputs == []


class TestNextflowDispatch:
    def test_nextflow_registered_in_registry(self):
        assert "nextflow" in _adapter_registry

    def test_dispatch_returns_nextflow_adapter(self):
        spec = _MockSpec(executors=["nextflow"])
        adapter = dispatch(spec)
        assert isinstance(adapter, NextflowAdapter)

    def test_dispatch_engine_override_nextflow(self):
        spec = _MockSpec(executors=["docker"])
        adapter = dispatch(spec, engine="nextflow")
        assert isinstance(adapter, NextflowAdapter)
