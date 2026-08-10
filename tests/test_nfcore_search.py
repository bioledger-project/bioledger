"""Tests for the nf-core modules search module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.nfcore_search import (
    NFCoreResult,
    download_module,
    parse_module_to_spec,
    search_nfcore,
)


class TestSearchNFCore:
    def test_search_returns_results(self):
        """search_nfcore() should return NFCoreResult objects."""
        mock_responses = [
            # 1. List categories
            MagicMock(status_code=200, json=lambda: [
                {"type": "dir", "name": "align"},
                {"type": "dir", "name": "variant_calling"},
            ]),
            # 2. List tools under "align"
            MagicMock(status_code=200, json=lambda: [
                {"type": "dir", "name": "bowtie2"},
                {"type": "dir", "name": "bwa"},
            ]),
            # 3. Fetch description for align/bowtie2 (called before next category)
            MagicMock(status_code=200, text="description: Fast alignment tool"),
            # 4. List tools under "variant_calling" (no match)
            MagicMock(status_code=200, json=lambda: [
                {"type": "dir", "name": "gatk"},
            ]),
        ]
        with patch("httpx.get", side_effect=mock_responses):
            results = search_nfcore("bowtie2")
        assert len(results) == 1
        assert isinstance(results[0], NFCoreResult)
        assert results[0].name == "bowtie2"
        assert results[0].subdirectory == "align/bowtie2"
        assert results[0].description == "Fast alignment tool"

    def test_search_no_match(self):
        """search_nfcore() with no matches returns empty list."""
        mock_responses = [
            # 1. List categories
            MagicMock(status_code=200, json=lambda: [
                {"type": "dir", "name": "align"},
            ]),
            # 2. List tools under "align" (no match)
            MagicMock(status_code=200, json=lambda: [
                {"type": "dir", "name": "bwa"},
            ]),
        ]
        with patch("httpx.get", side_effect=mock_responses):
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


class TestParseModuleToSpec:
    def test_parse_valid_module(self, tmp_path):
        """parse_module_to_spec() should extract process name, container, inputs, outputs."""
        nf_content = (
            "process BOWTIE2_ALIGN {\n"
            "    container 'quay.io/biocontainers/bowtie2:2.5.0'\n"
            "\n"
            "    input:\n"
            "    path reads\n"
            "    path index\n"
            "\n"
            "    output:\n"
            '    path "*.bam", emit: bam\n'
            '    path "*.log", emit: log\n'
            "\n"
            "    script:\n"
            '    """\n'
            "    bowtie2 -x ${index} -U ${reads} -S output.bam\n"
            '    """\n'
            "}\n"
        )
        (tmp_path / "main.nf").write_text(nf_content)

        spec = parse_module_to_spec(tmp_path)
        assert spec.name == "BOWTIE2_ALIGN"
        assert spec.container == "quay.io/biocontainers/bowtie2:2.5.0"
        assert "reads" in spec.inputs
        assert "index" in spec.inputs
        assert "bam" in spec.outputs
        assert "log" in spec.outputs
        assert spec.outputs["bam"].pattern == "*.bam"
        assert "nextflow" in spec.executors

    def test_parse_no_main_nf_raises(self, tmp_path):
        """parse_module_to_spec() should raise FileNotFoundError if no main.nf."""
        with pytest.raises(FileNotFoundError):
            parse_module_to_spec(tmp_path)

    def test_parse_minimal_module(self, tmp_path):
        """parse_module_to_spec() should handle a minimal process."""
        nf_content = (
            "process SIMPLE {\n"
            "    input:\n"
            "    path input_file\n"
            "\n"
            "    script:\n"
            '    """\n'
            "    echo hello\n"
            '    """\n'
            "}\n"
        )
        (tmp_path / "main.nf").write_text(nf_content)

        spec = parse_module_to_spec(tmp_path)
        assert spec.name == "SIMPLE"
        assert "input_file" in spec.inputs
        assert spec.outputs == {}
