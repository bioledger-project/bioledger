"""Tests for the Nextflow adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

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
        parameters=None,
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
        self.parameters = parameters or {}


class TestParseCollectionValue:
    def test_glob(self):
        kind, pattern = _parse_collection_value("data/*.bam")
        assert kind == "glob"
        assert pattern == "data/*.bam"

    def test_manifest(self):
        kind, pattern = _parse_collection_value("data/manifest.tsv")
        assert kind == "manifest"

    def test_entry_id(self):
        kind, pattern = _parse_collection_value("abc123def456abcd")
        assert kind == "entry_id"


class TestResolveContainer:
    def test_explicit_container(self):
        spec = _MockSpec(container="quay.io/biocontainers/samtools:1.17")
        assert _resolve_container(spec) == "quay.io/biocontainers/samtools:1.17"

    def test_conda_deps(self):
        spec = _MockSpec(container="", conda_deps=["samtools=1.17"])
        with patch(
            "bioledger.core.engines.conda_derive.derive_container_image",
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

    def test_combine_chains_at_same_level(self, tmp_path):
        """Verify combine() is chained on the scatter channel, not nested."""
        adapter = NextflowAdapter()
        spec = _MockSpec(
            inputs={
                "reads": _MockInput("reads", collection=True),
                "ref": _MockInput("ref", collection=False),
                "adapter": _MockInput("adapter", collection=False),
                "config": _MockInput("config", collection=False),
            },
        )
        handle = adapter.generate(
            spec=spec,
            input_mapping={
                "reads": "data/*.fastq",
                "ref": "/data/ref.fa",
                "adapter": "/data/adapters.fa",
                "config": "/data/config.txt",
            },
        )
        content = handle.artifact_path.read_text()
        # Extract the workflow call line
        wf_lines = [line.strip() for line in content.splitlines() if "samtools_sort(" in line]
        assert wf_lines, "Expected a process call in workflow"
        wf_call = wf_lines[0]
        # Should be: reads_ch.combine(ref_ch).combine(adapter_ch).combine(config_ch)
        assert wf_call.count(".combine(") == 3
        # No nested combine inside combine arguments
        import re
        combines = re.findall(r"\.combine\(([^)]+)\)", wf_call)
        for arg in combines:
            assert ".combine(" not in arg, f"Nested combine found in argument: {arg}"

    def test_combine_zero_single_inputs(self, tmp_path):
        """With only a collection input, no combine() should appear."""
        adapter = NextflowAdapter()
        spec = _MockSpec(
            inputs={"reads": _MockInput("reads", collection=True)},
        )
        handle = adapter.generate(
            spec=spec, input_mapping={"reads": "data/*.fastq"}
        )
        content = handle.artifact_path.read_text()
        wf_lines = [line.strip() for line in content.splitlines() if "samtools_sort(" in line]
        assert wf_lines
        assert ".combine(" not in wf_lines[0]

    def test_combine_one_single_input(self, tmp_path):
        """With 1 single input, should have exactly one non-nested combine()."""
        adapter = NextflowAdapter()
        spec = _MockSpec(
            inputs={
                "reads": _MockInput("reads", collection=True),
                "ref": _MockInput("ref", collection=False),
            },
        )
        handle = adapter.generate(
            spec=spec,
            input_mapping={"reads": "data/*.fastq", "ref": "/data/ref.fa"},
        )
        content = handle.artifact_path.read_text()
        wf_lines = [line.strip() for line in content.splitlines() if "samtools_sort(" in line]
        assert wf_lines
        assert wf_lines[0].count(".combine(") == 1
        import re
        combines = re.findall(r"\.combine\(([^)]+)\)", wf_lines[0])
        for arg in combines:
            assert ".combine(" not in arg

    def test_combine_two_single_inputs(self, tmp_path):
        """With 2 single inputs, should have two chained non-nested combine()s."""
        adapter = NextflowAdapter()
        spec = _MockSpec(
            inputs={
                "reads": _MockInput("reads", collection=True),
                "ref": _MockInput("ref", collection=False),
                "adapter": _MockInput("adapter", collection=False),
            },
        )
        handle = adapter.generate(
            spec=spec,
            input_mapping={
                "reads": "data/*.fastq",
                "ref": "/data/ref.fa",
                "adapter": "/data/adapters.fa",
            },
        )
        content = handle.artifact_path.read_text()
        wf_lines = [line.strip() for line in content.splitlines() if "samtools_sort(" in line]
        assert wf_lines
        assert wf_lines[0].count(".combine(") == 2
        import re
        combines = re.findall(r"\.combine\(([^)]+)\)", wf_lines[0])
        for arg in combines:
            assert ".combine(" not in arg


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


class TestNextflowSubmitSpec:
    def test_submit_spec_renders_command_and_returns_runhandle(self, tmp_path):
        """submit_spec() should Jinja-render spec.command and launch as subprocess."""
        adapter = NextflowAdapter()
        spec = _MockSpec(
            command="nextflow run main.nf --input {{ inputs.input }} --threads {{ parameters.threads }} --outdir {{ outputs._dir }}",
            inputs={
                "input": _MockInput("input", collection=False),
            },
        )
        with (
            patch.object(adapter, "_detect_version", return_value="23.10.0"),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            handle = adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={"threads": "4"},
                work_dir=tmp_path,
            )
        assert handle.executor == "nextflow"
        assert handle.run_id == "12345"
        assert handle.status == "running"
        # Verify the command was rendered and Popen was called
        mock_popen.assert_called_once()
        cmd_args = mock_popen.call_args[0][0]
        rendered = " ".join(cmd_args)
        assert "/data/file.bam" in rendered
        assert str(tmp_path) in rendered

    def test_submit_spec_no_command_raises(self, tmp_path):
        """submit_spec() should raise if spec has no command."""
        adapter = NextflowAdapter()
        spec = _MockSpec(command="")
        with pytest.raises(RuntimeError, match="no command field"):
            adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={},
                work_dir=tmp_path,
            )
