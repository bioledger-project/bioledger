"""Tests for the Snakemake adapter."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from bioledger.core.engines.dispatch import _adapter_registry, dispatch
from bioledger.core.engines.protocol import RunHandle
from bioledger.core.engines.snakemake_adapter import (
    SnakemakeAdapter,
    _parse_collection_value,
    _resolve_container,
)


class _MockInput:
    """Mock ToolInput."""

    def __init__(self, name: str = "", collection: bool = False, format: str = "any"):
        self.name = name
        self.collection = collection
        self.format = format


class _MockOutput:
    """Mock ToolOutput."""

    def __init__(self, name: str = "", pattern: str = "*.out", format: str = "any"):
        self.name = name
        self.pattern = pattern
        self.format = format


class _MockSpec:
    """Mock ExecutionSpec for Snakemake adapter tests."""

    def __init__(
        self,
        name: str = "samtools_sort",
        container: str = "quay.io/biocontainers/samtools:1.17",
        conda_deps: list[str] | None = None,
        command: str = "samtools sort -o {{ outputs.sorted_bam }} {{ inputs.bam }}",
        inputs: dict | None = None,
        outputs: dict | None = None,
        executors: list[str] | None = None,
    ):
        self.name = name
        self.container = container
        self.conda_deps = conda_deps or []
        self.command = command
        self.inputs = inputs or {"bam": _MockInput("bam", collection=True)}
        self.outputs = outputs or {"sorted_bam": _MockOutput("sorted_bam", "*.sorted.bam")}
        self.executors = executors or ["snakemake"]


class TestParseCollectionValue:
    def test_glob(self):
        kind, pattern = _parse_collection_value("data/*.bam")
        assert kind == "glob"
        assert pattern == "data/*.bam"

    def test_manifest(self):
        kind, pattern = _parse_collection_value("data/manifest.tsv")
        assert kind == "manifest"

    def test_entry_id(self):
        kind, pattern = _parse_collection_value("abc123def456abc123")
        assert kind == "entry_id"

    def test_plain_path(self):
        kind, pattern = _parse_collection_value("data/file.bam")
        assert kind == "glob"


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


class TestSnakemakeGenerate:
    def test_generate_produces_snakefile(self, tmp_path):
        adapter = SnakemakeAdapter()
        spec = _MockSpec()
        with patch.object(adapter, "_config"):
            handle = adapter.generate(
                spec=spec,
                input_mapping={"bam": "data/*.bam"},
            )
        assert handle.executor == "snakemake"
        assert handle.artifact_path.exists()
        content = handle.artifact_path.read_text()
        assert "rule all:" in content
        assert "rule samtools_sort:" in content
        assert "container:" in content
        assert "quay.io/biocontainers/samtools:1.17" in content
        assert "expand(" in content
        assert handle.step_inventory == ["samtools_sort"]

    def test_generate_with_conda_deps(self, tmp_path):
        adapter = SnakemakeAdapter()
        spec = _MockSpec(container="", conda_deps=["samtools=1.17"])
        with patch(
            "bioledger.core.engines.conda_derive.derive_container_image",
            return_value="quay.io/biocontainers/samtools:1.17--hdfd78af_0",
        ):
            handle = adapter.generate(
                spec=spec,
                input_mapping={"bam": "data/*.bam"},
            )
        content = handle.artifact_path.read_text()
        assert "quay.io/biocontainers/samtools:1.17--hdfd78af_0" in content

    def test_generate_no_container(self, tmp_path):
        adapter = SnakemakeAdapter()
        spec = _MockSpec(container="", conda_deps=[])
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam"},
        )
        content = handle.artifact_path.read_text()
        assert "container:" not in content

    def test_generate_single_input(self, tmp_path):
        """Non-collection inputs become params, not scatter channels."""
        adapter = SnakemakeAdapter()
        spec = _MockSpec(
            inputs={
                "bam": _MockInput("bam", collection=True),
                "ref": _MockInput("ref", collection=False),
            },
        )
        handle = adapter.generate(
            spec=spec,
            input_mapping={"bam": "data/*.bam", "ref": "/data/ref.fa"},
        )
        content = handle.artifact_path.read_text()
        assert "ref" in content


class TestSnakemakePoll:
    def test_poll_invalid_pid(self):
        adapter = SnakemakeAdapter()
        rh = RunHandle(
            executor="snakemake",
            run_id="not-a-pid",
            entry_id="e1",
            work_dir="/tmp",
        )
        assert adapter.poll(rh) is None

    def test_poll_completed_with_stats(self, tmp_path):
        """When process is gone and stats.json exists, read completion from it."""
        adapter = SnakemakeAdapter()
        # Write a stats.json
        stats = {
            "samtools_sort": {"jobs": 2, "exit-code": {"0": 2}},
            "total": {"jobs": 2, "exit-code": {"0": 2}},
        }
        (tmp_path / "stats.json").write_text(json.dumps(stats))

        # Use a PID that doesn't exist (will trigger ProcessLookupError)
        rh = RunHandle(
            executor="snakemake",
            run_id="99999999",
            entry_id="e1",
            work_dir=tmp_path,
        )
        result = adapter.poll(rh)
        assert result is not None
        assert result.state == "completed"
        assert result.total == 2
        assert result.completed == 2

    def test_poll_failed_with_stats(self, tmp_path):
        adapter = SnakemakeAdapter()
        stats = {
            "samtools_sort": {"jobs": 2, "exit-code": {"0": 1, "1": 1}},
            "total": {"jobs": 2, "exit-code": {"0": 1, "1": 1}},
        }
        (tmp_path / "stats.json").write_text(json.dumps(stats))

        rh = RunHandle(
            executor="snakemake",
            run_id="99999999",
            entry_id="e1",
            work_dir=tmp_path,
        )
        result = adapter.poll(rh)
        assert result is not None
        assert result.state == "failed"
        assert "1/2 jobs failed" in result.message

    def test_poll_still_running(self, tmp_path):
        """When process exists, return running status."""
        adapter = SnakemakeAdapter()
        # Use our own PID — it exists
        rh = RunHandle(
            executor="snakemake",
            run_id=str(os.getpid()),
            entry_id="e1",
            work_dir=tmp_path,
        )
        result = adapter.poll(rh)
        assert result is not None
        assert result.state == "running"


class TestSnakemakeCollect:
    def test_collect_returns_empty_for_now(self, tmp_path):
        adapter = SnakemakeAdapter()
        rh = RunHandle(
            executor="snakemake",
            run_id="job-1",
            entry_id="e1",
            work_dir=tmp_path,
        )
        result = adapter.collect(rh)
        assert result == []


class TestDispatchWithSnakemake:
    def test_snakemake_registered(self):
        assert "snakemake" in _adapter_registry

    def test_dispatch_returns_snakemake_adapter(self):
        spec = _MockSpec(executors=["snakemake"])
        adapter = dispatch(spec)
        assert isinstance(adapter, SnakemakeAdapter)

    def test_dispatch_snakemake_with_config(self):
        from bioledger.core.engines.config import EnginesConfig, SnakemakeEngineConfig
        spec = _MockSpec(executors=["snakemake"])
        cfg = EnginesConfig(
            snakemake=SnakemakeEngineConfig(extra_args=["--cores", "4"])
        )
        adapter = dispatch(spec, config=cfg)
        assert isinstance(adapter, SnakemakeAdapter)
        assert adapter._config.extra_args == ["--cores", "4"]


class TestSnakemakeSubmitSpec:
    def test_submit_spec_renders_command_and_returns_runhandle(self, tmp_path):
        """submit_spec() should Jinja-render spec.command and launch as subprocess."""
        adapter = SnakemakeAdapter()
        spec = _MockSpec(
            command=(
                "snakemake -s Snakefile --configfile config.yaml "
                "--input {{ inputs.input }} --threads {{ parameters.threads }} "
                "--outdir {{ outputs._dir }}"
            ),
            inputs={"input": _MockInput("input", collection=False)},
        )
        with (
            patch.object(
                type(adapter), "executable",
                new_callable=lambda: "snakemake",
            ),
            patch.object(adapter, "_detect_version", return_value="7.32.4"),
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
        assert handle.executor == "snakemake"
        assert handle.run_id == "12345"
        assert handle.status == "running"
        mock_popen.assert_called_once()
        cmd_args = mock_popen.call_args[0][0]
        rendered = " ".join(cmd_args)
        assert "/data/file.bam" in rendered
        assert str(tmp_path) in rendered

    def test_submit_spec_no_command_raises(self, tmp_path):
        """submit_spec() should raise if spec has no command."""
        adapter = SnakemakeAdapter()
        spec = _MockSpec(command="")
        with pytest.raises(RuntimeError, match="no command field"):
            adapter.submit_spec(
                spec=spec,
                input_mapping={"input": "/data/file.bam"},
                params={},
                work_dir=tmp_path,
            )
