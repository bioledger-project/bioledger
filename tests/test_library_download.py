"""Tests for StudyLibrary.download() async method."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from bioledger.core.library import StudyLibrary


@pytest.mark.asyncio
class TestStudyLibraryDownload:
    """Tests for StudyLibrary.download() async method."""

    async def test_download_raises_if_study_not_found(self, tmp_path: Path):
        """download() raises ValueError if study not in index."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "study_index.json").write_text(json.dumps([]))

        lib = StudyLibrary(cache_dir=cache_dir)

        with pytest.raises(ValueError, match="not found"):
            await lib.download("NONEXISTENT", tmp_path / "dest")

    async def test_download_fetches_manifest_and_isa_files(self, tmp_path: Path):
        """download() fetches manifest.yaml and isa_files."""
        cache_dir = tmp_path / "cache"
        dest_dir = tmp_path / "dest"
        cache_dir.mkdir()

        # Setup index with isa_files
        entries = [{
            "accession": "PRJNA123",
            "path": "PRJNA123",
            "isa_files": ["i_investigation.txt", "s_study.txt"]
        }]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        lib = StudyLibrary(cache_dir=cache_dir)

        # Mock httpx.get for manifest and isa files
        def mock_get(url, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            if "manifest.yaml" in url:
                mock_response.text = "study_type: experimental_data\nfiles: []"
            else:
                mock_response.text = "mock content"
            return mock_response

        # Mock the download_manifest to avoid real network
        with patch("httpx.get", side_effect=mock_get):
            with patch("bioledger_isatab_schema.manifest.load_manifest") as mock_load:
                with patch("bioledger_isatab_schema.download.download_manifest") as mock_dl:
                    mock_manifest = Mock()
                    mock_load.return_value = mock_manifest
                    mock_dl.return_value = []  # No downloaded files

                    result = await lib.download("PRJNA123", dest_dir, with_data=False)

                    assert result == dest_dir
                    assert (dest_dir / "manifest.yaml").exists()
                    assert (dest_dir / "i_investigation.txt").exists()
                    assert (dest_dir / "s_study.txt").exists()
                    # download_manifest not called since with_data=False
                    mock_dl.assert_not_called()

    async def test_download_with_data_calls_download_manifest(self, tmp_path: Path):
        """download() calls download_manifest when with_data=True."""
        cache_dir = tmp_path / "cache"
        dest_dir = tmp_path / "dest"
        cache_dir.mkdir()

        entries = [{
            "accession": "PRJNA456",
            "path": "PRJNA456",
            "isa_files": []
        }]
        (cache_dir / "study_index.json").write_text(json.dumps(entries))

        lib = StudyLibrary(cache_dir=cache_dir)

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_response.text = "study_type: test\nfiles: []"
            mock_get.return_value = mock_response

            with patch("bioledger_isatab_schema.manifest.load_manifest") as mock_load:
                with patch("bioledger_isatab_schema.download.download_manifest") as mock_dl:
                    mock_manifest = Mock()
                    mock_load.return_value = mock_manifest
                    mock_dl.return_value = []

                    await lib.download("PRJNA456", dest_dir, with_data=True)

                    # download_manifest called with user_confirmed=True
                    mock_dl.assert_called_once()
                    call_args = mock_dl.call_args
                    assert call_args.kwargs.get("user_confirmed") is True
