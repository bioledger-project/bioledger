"""Tests for the nf-core modules search module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.nfcore_search import (
    NFCoreResult,
    download_module,
    generate_nfcore_wrapper,
    parse_meta_yml,
    search_nfcore,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nfcore"


def _tree_payload(*subdirs: str) -> dict:
    return {
        "tree": [
            {"path": f"modules/nf-core/{s}/meta.yml", "type": "blob"}
            for s in subdirs
        ]
    }


class TestSearchNFCore:
    def test_search_returns_results(self, tmp_path):
        """search_nfcore() uses the git Trees API and matches subdirs."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload(
                "bowtie2/align", "bowtie2/build", "fastqc", "gatk4/haplotypecaller",
            )),
            # _fetch_description("bowtie2/align")
            MagicMock(status_code=200, text="description: Align reads\n"),
            # _fetch_description("bowtie2/build")
            MagicMock(status_code=200, text="description: Build index\n"),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
            results = search_nfcore("bowtie2")
        assert len(results) == 2
        assert all(isinstance(r, NFCoreResult) for r in results)
        by_subdir = {r.subdirectory: r for r in results}
        assert set(by_subdir) == {"bowtie2/align", "bowtie2/build"}
        assert by_subdir["bowtie2/align"].name == "align"
        assert by_subdir["bowtie2/align"].description == "Align reads"

    def test_search_flat_and_nested_layouts(self, tmp_path):
        """Both flat (fastqc) and nested (bowtie2/align) dirs are modules."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload(
                "fastqc", "bowtie2/align",
            )),
            MagicMock(status_code=200, text="description: QC\n"),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
            results = search_nfcore("fastqc")
        assert [r.subdirectory for r in results] == ["fastqc"]

    def test_search_no_match(self, tmp_path):
        """search_nfcore() with no matches returns empty list."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload("bwa/mem")),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
            results = search_nfcore("nonexistent_tool")
        assert results == []


class TestDownloadModule:
    def test_download_module_creates_dir(self, tmp_path):
        """download_module() should create the destination directory."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "process foo { }"
        with patch("httpx.get", return_value=mock_resp):
            dest = download_module("align/bowtie2", dest_dir=tmp_path)
        assert dest.exists()
        assert (dest / "main.nf").exists()
        assert (dest / "meta.yml").exists()


class TestParseMetaYml:
    def test_parses_real_bowtie2_meta(self):
        """parse_meta_yml handles the real nested-channel meta.yml schema."""
        meta = parse_meta_yml(FIXTURES / "bowtie2_align")
        assert meta["process_name"] == "BOWTIE2_ALIGN"
        assert meta["inputs"] == ["reads", "index", "fasta"]
        # Map channels must be detected by type (meta, meta2, meta3), not name
        kinds = [(c["name"], c["kind"], c["has_meta"])
                 for c in meta["input_channels"]]
        assert kinds == [
            ("reads", "file", True),
            ("index", "file", True),
            ("fasta", "file", True),
            ("save_unaligned", "val", False),
            ("sort_bam", "val", False),
        ]
        assert meta["parameters"] == {
            "save_unaligned": False, "sort_bam": False,
        }
        assert meta["outputs"]["bam"] == "*.bam"
        assert "versions_bowtie2" not in meta["outputs"]
        # Structured containers.docker["linux/amd64"].name
        assert meta["container"].startswith(
            "community.wave.seqera.io/library/bowtie2"
        )

    def test_parses_real_fastqc_meta(self):
        """Brace patterns prefer the clean emit key; flat layout works."""
        meta = parse_meta_yml(FIXTURES / "fastqc")
        assert meta["process_name"] == "FASTQC"
        assert meta["inputs"] == ["reads"]
        # "*_{fastqc.html}" brace pattern — dict key "*.html" wins
        assert meta["outputs"] == {"html": "*.html", "zip": "*.zip"}
        assert "fastqc:" in meta["container"]


class TestGenerateNfcoreWrapper:
    def test_wrapper_includes_module_and_channels(self, tmp_path):
        """generate_nfcore_wrapper() should produce a valid Nextflow wrapper."""
        meta = {
            "inputs": ["reads", "index"],
            "outputs": {"bam": "*.bam"},
            "container": "quay.io/biocontainers/bowtie2:2.5.0",
            "process_name": "BOWTIE2_ALIGN",
        }
        wrapper, config = generate_nfcore_wrapper(
            "bowtie2/align",
            tmp_path,
            meta,
            "test_entry_001",
        )
        assert "include" in wrapper
        assert "BOWTIE2_ALIGN" in wrapper
        assert "reads_ch" in wrapper
        assert "index_ch" in wrapper
        assert "workflow {" in wrapper
        # publishDir is a process directive — it must live in the
        # companion config via withName, not in the top-level script
        assert "publishDir" not in wrapper
        assert "withName: 'BOWTIE2_ALIGN'" in config
        assert "publishDir = [path: params.outdir, mode: 'copy']" in config

    def test_wrapper_val_params_go_to_config_not_script(self, tmp_path):
        """val channel defaults must be config params (CLI-overridable)."""
        meta = parse_meta_yml(FIXTURES / "bowtie2_align")
        wrapper, config = generate_nfcore_wrapper(
            "bowtie2/align", tmp_path, meta, "e1",
        )
        # Script passes params positionally; defaults in config so that
        # `nextflow run --save_unaligned true` actually overrides them.
        assert "params.save_unaligned = " not in wrapper
        assert "params.save_unaligned" in wrapper  # positional call arg
        assert "sort_bam = false" in config
        # 5-arg call matches the module's real input block
        assert (
            "BOWTIE2_ALIGN(reads_ch, index_ch, fasta_ch, "
            "params.save_unaligned, params.sort_bam)" in wrapper
        )

    def test_wrapper_no_inputs_uses_fallback_channel(self, tmp_path):
        """generate_nfcore_wrapper() with no inputs should use input_ch."""
        meta = {
            "inputs": [],
            "outputs": {"out": "*.out"},
            "container": "",
            "process_name": "SOMETOOL",
        }
        wrapper, config = generate_nfcore_wrapper(
            "utils/sometool",
            tmp_path,
            meta,
            "test_entry_002",
        )
        assert "input_ch" in wrapper
        assert "SOMETOOL" in wrapper
        assert config  # outputs declared → publishDir config emitted

    def test_wrapper_no_outputs_emits_empty_config(self, tmp_path):
        """Modules with no outputs and no val params need no config."""
        meta = {
            "inputs": ["reads"],
            "outputs": {},
            "container": "",
            "process_name": "SOMETOOL",
        }
        wrapper, config = generate_nfcore_wrapper(
            "utils/sometool",
            tmp_path,
            meta,
            "test_entry_003",
        )
        assert "SOMETOOL" in wrapper
        assert config == ""
