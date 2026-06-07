"""Tests for LibraryClient, ToolLibrary, and StudyLibrary."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

from bioledger.core.library import LibraryClient, StudyLibrary, ToolLibrary


class TestLibraryClient:
    """Tests for the generic LibraryClient."""

    def test_refresh_fetches_and_caches(self, tmp_path: Path):
        """refresh() fetches from URL and writes to cache."""
        cache_path = tmp_path / "index.json"
        entries = [{"name": "tool1"}, {"name": "tool2"}]

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = entries
            mock_get.return_value = mock_response

            client = LibraryClient("http://example.com/index.json", cache_path)
            result = client.refresh()

            assert result == entries
            assert cache_path.exists()
            assert json.loads(cache_path.read_text()) == entries

    def test_refresh_uses_stale_cache_on_failure(self, tmp_path: Path):
        """On fetch failure, falls back to stale cache if available."""
        cache_path = tmp_path / "index.json"
        stale_entries = [{"name": "cached"}]
        cache_path.write_text(json.dumps(stale_entries))

        with patch("httpx.get") as mock_get:
            mock_get.side_effect = httpx.RequestError("Network down")

            client = LibraryClient("http://example.com/index.json", cache_path)
            result = client.refresh()

            # Falls back to stale cache
            assert result == stale_entries

    def test_entries_returns_cached_without_fetch(self, tmp_path: Path):
        """entries() returns cached data without network if fresh."""
        cache_path = tmp_path / "index.json"
        cached = [{"name": "cached_tool"}]
        cache_path.write_text(json.dumps(cached))

        # Make cache "fresh" (recent mtime)
        client = LibraryClient("http://example.com/index.json", cache_path, ttl=3600)

        with patch("httpx.get") as mock_get:
            result = client.entries()
            # Should not fetch
            mock_get.assert_not_called()
            assert result == cached

    def test_entries_refreshes_stale_cache(self, tmp_path: Path):
        """entries() refreshes if cache is stale."""
        cache_path = tmp_path / "index.json"
        cache_path.write_text(json.dumps([{"name": "old"}]))

        # Set mtime in the past to make it stale
        old_time = time.time() - 7200  # 2 hours ago
        cache_path.chmod(0o644)
        import os

        os.utime(cache_path, (old_time, old_time))

        new_entries = [{"name": "new"}]

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = new_entries
            mock_get.return_value = mock_response

            client = LibraryClient("http://example.com/index.json", cache_path, ttl=3600)
            result = client.entries()

            assert result == new_entries
            mock_get.assert_called_once()


class TestToolLibrary:
    """Tests for ToolLibrary high-level interface."""

    def test_list_cached_reads_cache_only(self, tmp_path: Path):
        """list_cached() reads from cache file without network."""
        cache_dir = tmp_path
        entries = [{"name": "samtools-faidx", "family": "samtools"}]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        lib = ToolLibrary(cache_dir=cache_dir)

        with patch("httpx.get") as mock_get:
            result = lib.list_cached()
            mock_get.assert_not_called()  # No network
            assert result == entries

    def test_list_cached_returns_empty_if_no_cache(self, tmp_path: Path):
        """list_cached() returns [] if no cache exists."""
        lib = ToolLibrary(cache_dir=tmp_path)
        result = lib.list_cached()
        assert result == []

    def test_search_finds_by_name(self, tmp_path: Path):
        """search() finds tools by name substring."""
        cache_dir = tmp_path
        entries = [
            {"name": "samtools-faidx", "family": "samtools"},
            {"name": "bwa-mem", "family": "bwa"},
        ]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        lib = ToolLibrary(cache_dir=cache_dir)
        results = lib.search("samtools")

        assert len(results) == 1
        assert results[0]["name"] == "samtools-faidx"

    def test_get_finds_exact_name(self, tmp_path: Path):
        """get() finds tool by exact name match."""
        cache_dir = tmp_path
        entries = [{"name": "samtools-faidx"}, {"name": "bwa-mem"}]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        lib = ToolLibrary(cache_dir=cache_dir)
        result = lib.get("bwa-mem")

        assert result is not None
        assert result["name"] == "bwa-mem"

    def test_get_returns_none_for_missing(self, tmp_path: Path):
        """get() returns None for non-existent tool."""
        cache_dir = tmp_path
        (cache_dir / "toolspec_index.json").write_text(json.dumps([{"name": "tool1"}]))

        lib = ToolLibrary(cache_dir=cache_dir)
        result = lib.get("nonexistent")

        assert result is None


class TestStudyLibrary:
    """Tests for StudyLibrary high-level interface."""

    def test_search_finds_by_organism(self, tmp_path: Path):
        """search() finds studies by organism."""
        cache_dir = tmp_path
        entries = [
            {"accession": "PRJNA123", "organism": "Plasmodium falciparum"},
            {"accession": "PRJNA456", "organism": "Leishmania donovani"},
        ]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        lib = StudyLibrary(cache_dir=cache_dir)
        results = lib.search("Leishmania")

        assert len(results) == 1
        assert results[0]["accession"] == "PRJNA456"

    def test_get_finds_by_accession(self, tmp_path: Path):
        """get() finds study by exact accession."""
        cache_dir = tmp_path
        entries = [{"accession": "GCF_123"}, {"accession": "GCF_456"}]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        lib = StudyLibrary(cache_dir=cache_dir)
        result = lib.get("GCF_456")

        assert result is not None
        assert result["accession"] == "GCF_456"
