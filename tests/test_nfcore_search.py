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


class TestGenerateNfcoreWrapper:
    def test_wrapper_includes_module_and_channels(self, tmp_path):
        """generate_nfcore_wrapper() should produce a valid Nextflow wrapper."""
        meta = {
            "inputs": ["reads", "index"],
            "outputs": {"bam": "*.bam"},
            "container": "quay.io/biocontainers/bowtie2:2.5.0",
        }
        wrapper = generate_nfcore_wrapper(
            "align/bowtie2/bowtie2_align",
            tmp_path,
            meta,
            "test_entry_001",
        )
        assert "include" in wrapper
        assert "bowtie2_align" in wrapper
        assert "reads_ch" in wrapper
        assert "index_ch" in wrapper
        assert "workflow {" in wrapper
        assert "publishDir" in wrapper

    def test_wrapper_no_inputs_uses_fallback_channel(self, tmp_path):
        """generate_nfcore_wrapper() with no inputs should use input_ch."""
        meta = {
            "inputs": [],
            "outputs": {"out": "*.out"},
            "container": "",
        }
        wrapper = generate_nfcore_wrapper(
            "utils/sometool",
            tmp_path,
            meta,
            "test_entry_002",
        )
        assert "input_ch" in wrapper
        assert "sometool" in wrapper
