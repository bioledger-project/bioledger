"""Tests for CLI library and study commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from bioledger.apps.cli.main import app

runner = CliRunner()


class TestLibraryCommands:
    """Tests for 'bioledger library' CLI commands."""

    def test_library_list_shows_tools(self, tmp_path: Path, monkeypatch):
        """library list shows tools from index."""
        # Setup cache with mock data
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [
            {"name": "tool1", "family": "fam1", "version": "1.0",
             "description": "desc", "categories": []},
            {"name": "tool2", "family": "fam2", "version": "2.0",
             "description": "desc2", "categories": []},
        ]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        # Mock config to use temp dir
        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["library", "list"])

        assert result.exit_code == 0
        assert "tool1" in result.output
        assert "tool2" in result.output

    def test_library_search_finds_tools(self, tmp_path: Path):
        """library search filters by query."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [
            {"name": "samtools-view", "family": "samtools"},
            {"name": "bwa-mem", "family": "bwa"},
        ]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["library", "search", "samtools"])

        assert result.exit_code == 0
        assert "samtools-view" in result.output
        assert "bwa-mem" not in result.output

    def test_library_show_displays_details(self, tmp_path: Path):
        """library show displays tool details."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [{
            "name": "test-tool",
            "family": "test",
            "version": "1.0",
            "description": "A test tool",
            "container": "test:latest",
            "categories": ["cat1"],
            "inputs": ["input1"],
            "outputs": ["output1"],
            "path": "test/test-tool",
        }]
        (cache_dir / "toolspec_index.json").write_text(json.dumps(entries))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["library", "show", "test-tool"])

        assert result.exit_code == 0
        assert "test-tool" in result.output
        assert "A test tool" in result.output
        assert "test:latest" in result.output

    def test_library_show_exits_on_missing(self, tmp_path: Path):
        """library show exits with error for missing tool."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "toolspec_index.json").write_text(json.dumps([]))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["library", "show", "missing"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestStudyCommands:
    """Tests for 'bioledger study' CLI commands."""

    def test_study_list_shows_studies(self, tmp_path: Path):
        """study list shows studies from index."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [
            {"accession": "PRJNA1", "organism": "P. falciparum", "study_type": "rnaseq"},
            {"accession": "PRJNA2", "organism": "L. donovani", "study_type": "wgs"},
        ]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["study", "list"])

        assert result.exit_code == 0
        assert "PRJNA1" in result.output
        assert "PRJNA2" in result.output

    def test_study_search_finds_by_organism(self, tmp_path: Path):
        """study search filters by organism."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [
            {"accession": "GCF_1", "organism": "Plasmodium falciparum"},
            {"accession": "GCF_2", "organism": "Leishmania donovani"},
        ]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            result = runner.invoke(app, ["study", "search", "Leishmania"])

        assert result.exit_code == 0
        assert "GCF_2" in result.output
        assert "GCF_1" not in result.output

    def test_study_load_calls_download(self, tmp_path: Path):
        """study load calls StudyLibrary.download()."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        entries = [{"accession": "PRJNA123", "path": "PRJNA123", "isa_files": []}]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        with patch("bioledger.config.BioLedgerConfig") as mock_config:
            mock_config.return_value.home_dir = tmp_path
            with patch("bioledger.core.library.StudyLibrary.download") as mock_download:
                mock_download.return_value = tmp_path / "datasets" / "PRJNA123"

                result = runner.invoke(app, ["study", "load", "PRJNA123", "--download"])

                assert result.exit_code == 0
                mock_download.assert_called_once()
                call_args = mock_download.call_args
                assert call_args[0][0] == "PRJNA123"  # accession
                assert call_args[1].get("with_data") is True
