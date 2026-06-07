"""Tests for ToolStore.import_from_library()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bioledger_toolspec_schema.store import ToolStore


class TestToolStoreImportFromLibrary:
    """Tests for importing specs from remote library."""

    def test_import_from_library_fetches_and_saves(self, tmp_path: Path):
        """import_from_library() fetches spec YAML and saves to store."""
        store = ToolStore(tools_dir=tmp_path)

        spec_yaml = """
execution:
  name: test-tool
  container: test:latest
  command: "echo hello"
  inputs: {}
  outputs: {}
  status: valid
"""

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = spec_yaml
            mock_get.return_value = mock_response

            spec = store.import_from_library(path="test/test-tool", ref="main")

            assert spec.name == "test-tool"
            assert spec.container == "test:latest"
            # Should be saved to store
            assert store.has("test-tool")

    def test_import_from_library_raises_on_fetch_failure(self, tmp_path: Path):
        """import_from_library() raises ValueError on network failure."""
        store = ToolStore(tools_dir=tmp_path)

        with patch("httpx.get") as mock_get:
            mock_get.side_effect = Exception("Network error")

            with pytest.raises(ValueError, match="Failed to fetch"):
                store.import_from_library(path="test/bad-tool", ref="main")

    def test_import_from_library_raises_on_invalid_yaml(self, tmp_path: Path):
        """import_from_library() raises ValueError on invalid spec."""
        store = ToolStore(tools_dir=tmp_path)

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = "invalid: yaml: ["  # Invalid YAML
            mock_get.return_value = mock_response

            with pytest.raises(ValueError, match="Failed to parse"):
                store.import_from_library(path="test/bad-tool", ref="main")

    def test_import_from_library_uses_custom_ref(self, tmp_path: Path):
        """import_from_library() uses provided git ref in URL."""
        store = ToolStore(tools_dir=tmp_path)

        spec_yaml = """
execution:
  name: versioned-tool
  container: test:latest
  command: "echo hello"
  inputs: {}
  outputs: {}
  status: valid
"""

        with patch("httpx.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = spec_yaml
            mock_get.return_value = mock_response

            spec = store.import_from_library(
                path="family/tool", ref="v1.0.0"
            )

            # Verify URL contains the ref
            call_args = mock_get.call_args
            assert "v1.0.0" in call_args[0][0]
