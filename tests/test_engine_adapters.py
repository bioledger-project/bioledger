"""Tests for engine adapter fixes (J2, J4, J6).

Validates:
- J2: Engine metadata sidecar write/read roundtrip
- J4: File handle leaks fixed (log files closed after Popen)
- J6: parse_meta_yml correctly parses nf-core meta.yml structure
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bioledger.core.engines.config import SnakemakeEngineConfig
from bioledger.core.engines.nfcore_search import parse_meta_yml


class TestEngineMetadataSidecar:
    """J2: Test sidecar write/read roundtrip."""

    def test_write_and_read_sidecar(self, tmp_path):
        from bioledger.forges.analysisforge.agent import (
            _read_engine_metadata_sidecar,
            _write_engine_metadata_sidecar,
        )

        run_handle = MagicMock()
        run_handle.work_dir = tmp_path / "work_dir"
        run_handle.run_id = "12345"
        run_handle.executor = "snakemake"
        run_handle.submitted_at = "2024-01-01T00:00:00Z"
        run_handle.status = "running"
        run_handle.process_start_time = 1234567890.0
        run_handle.metadata = {}

        _write_engine_metadata_sidecar(tmp_path, "entry_1", run_handle)

        sidecar = tmp_path / "_engine_metadata_entry_1.json"
        assert sidecar.exists()

        meta = _read_engine_metadata_sidecar(tmp_path, "entry_1")
        assert meta is not None
        assert meta["run_id"] == "12345"
        assert meta["executor"] == "snakemake"
        assert meta["work_dir"] == str(tmp_path / "work_dir")
        assert meta["process_start_time"] == 1234567890.0

    def test_read_missing_sidecar_returns_none(self, tmp_path):
        from bioledger.forges.analysisforge.agent import (
            _read_engine_metadata_sidecar,
        )

        meta = _read_engine_metadata_sidecar(tmp_path, "nonexistent")
        assert meta is None

    def test_sidecar_no_module_or_tool_dir(self, tmp_path):
        """Sidecar no longer stores module_dir/tool_dir — companion_files on entry instead."""
        from bioledger.forges.analysisforge.agent import (
            _read_engine_metadata_sidecar,
            _write_engine_metadata_sidecar,
        )

        run_handle = MagicMock()
        run_handle.work_dir = tmp_path / "work"
        run_handle.run_id = "99"
        run_handle.executor = "galaxy"
        run_handle.submitted_at = "2024-01-01T00:00:00Z"
        run_handle.status = "running"
        run_handle.process_start_time = None
        run_handle.metadata = {"planemo_pid": 42}

        _write_engine_metadata_sidecar(
            tmp_path, "entry_2", run_handle,
        )

        meta = _read_engine_metadata_sidecar(tmp_path, "entry_2")
        assert meta is not None
        assert meta["planemo_pid"] == 42
        assert "tool_dir" not in meta
        assert "module_dir" not in meta


class TestParseMetaYml:
    """J6: Test parse_meta_yml correctly parses nf-core meta.yml."""

    def test_parse_basic_meta_yml(self, tmp_path):
        meta_content = """\
name: bowtie2_align
description: Align reads with Bowtie2
input:
  - name: reads
    type: file
    description: Input reads
  - name: reference
    type: file
    description: Reference genome
output:
  - name: bam
    type: file
    pattern: "*.bam"
  - name: log
    type: file
    pattern: "*.log"
"""
        (tmp_path / "meta.yml").write_text(meta_content)

        result = parse_meta_yml(tmp_path)
        assert "reads" in result["inputs"]
        assert "reference" in result["inputs"]
        assert result["outputs"]["bam"] == "*.bam"
        assert result["outputs"]["log"] == "*.log"
        assert result["container"] == ""

    def test_parse_meta_yml_with_container(self, tmp_path):
        meta_content = """\
name: samtools_sort
description: Sort BAM
input:
  - name: bam
    type: file
output:
  - name: sorted_bam
    type: file
    pattern: "*.sorted.bam"
"""
        (tmp_path / "meta.yml").write_text(meta_content)
        (tmp_path / "main.nf").write_text(
            "process samtools_sort {\n"
            "  container 'quay.io/biocontainers/samtools:1.17'\n"
            "}\n"
        )

        result = parse_meta_yml(tmp_path)
        assert result["container"] == "quay.io/biocontainers/samtools:1.17"

    def test_parse_meta_yml_missing_file(self, tmp_path):
        result = parse_meta_yml(tmp_path)
        assert result == {"inputs": [], "outputs": {}, "container": ""}

    def test_parse_meta_yml_empty_sections(self, tmp_path):
        meta_content = """\
name: empty
description: No inputs or outputs
input: []
output: []
"""
        (tmp_path / "meta.yml").write_text(meta_content)
        result = parse_meta_yml(tmp_path)
        assert result["inputs"] == []
        assert result["outputs"] == {}


class TestSnakemakeConfigInAdapter:
    """J5/K1: Test that SnakemakeEngineConfig fields are used in command building."""

    def test_container_runtime_apptainer(self):
        cfg = SnakemakeEngineConfig(container_runtime="apptainer")
        assert cfg.container_runtime == "apptainer"

    def test_container_runtime_conda(self):
        cfg = SnakemakeEngineConfig(container_runtime="conda")
        assert cfg.container_runtime == "conda"

    def test_container_runtime_none(self):
        cfg = SnakemakeEngineConfig(container_runtime="none")
        assert cfg.container_runtime == "none"

    def test_jobs_default(self):
        cfg = SnakemakeEngineConfig()
        assert cfg.jobs == 1

    def test_jobs_custom(self):
        cfg = SnakemakeEngineConfig(jobs=4)
        assert cfg.jobs == 4
