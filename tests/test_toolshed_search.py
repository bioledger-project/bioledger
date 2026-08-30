"""Tests for the Galaxy Tool Shed search module."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.toolshed_search import (
    ToolShedResult,
    download_tool,
    parse_tool_to_spec,
    search_toolshed,
)


def _mock_bioblend():
    """Create a mock bioblend module with ToolShedInstance."""
    mock_module = types.ModuleType("bioblend")
    mock_toolshed = types.ModuleType("bioblend.toolshed")
    mock_ts_cls = MagicMock()
    mock_toolshed.ToolShedInstance = mock_ts_cls
    mock_module.toolshed = mock_toolshed
    return mock_module, mock_toolshed, mock_ts_cls


class TestSearchToolshed:
    def test_search_returns_results(self):
        """search_toolshed() should return ToolShedResult objects."""
        mock_module, mock_toolshed, mock_ts_cls = _mock_bioblend()
        mock_ts = MagicMock()
        mock_ts_cls.return_value = mock_ts
        mock_ts.tools.search_tools.return_value = [
            {
                "name": "Bowtie2",
                "description": "Fast and sensitive read alignment",
                "repo_owner": "devteam",
                "repo_name": "bowtie2",
                "id": "devteam/bowtie2",
            }
        ]
        with patch.dict(sys.modules, {"bioblend": mock_module, "bioblend.toolshed": mock_toolshed}):
            results = search_toolshed("bowtie2")
        assert len(results) == 1
        assert isinstance(results[0], ToolShedResult)
        assert results[0].name == "Bowtie2"
        assert results[0].repo_owner == "devteam"
        assert results[0].repo_name == "bowtie2"

    def test_search_empty_query(self):
        """search_toolshed() with empty results returns empty list."""
        mock_module, mock_toolshed, mock_ts_cls = _mock_bioblend()
        mock_ts = MagicMock()
        mock_ts_cls.return_value = mock_ts
        mock_ts.tools.search_tools.return_value = []
        with patch.dict(sys.modules, {"bioblend": mock_module, "bioblend.toolshed": mock_toolshed}):
            results = search_toolshed("nonexistent_tool_xyz")
        assert results == []


class TestDownloadTool:
    def test_download_tool_creates_dir(self, tmp_path):
        """download_tool() should create the destination directory."""
        mock_module, mock_toolshed, mock_ts_cls = _mock_bioblend()
        mock_ts = MagicMock()
        mock_ts_cls.return_value = mock_ts
        mock_ts.repositories.show_repository.return_value = {
            "repo_revisions": [{"changeset_revision": "abc123"}]
        }
        mock_ts.repositories.get_repository_revision_install_info.return_value = [
            {"file_path": "bowtie2.xml"},
            {"file_path": "macros.xml"},
        ]
        with (
            patch.dict(sys.modules, {"bioblend": mock_module, "bioblend.toolshed": mock_toolshed}),
            patch("httpx.get") as mock_get,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<tool id='test'/>"
            mock_get.return_value = mock_resp
            dest, rev = download_tool("devteam", "bowtie2", dest_dir=tmp_path)
        assert dest.exists()
        assert dest == tmp_path
        assert rev == "abc123"
        assert mock_get.call_count == 2

    def test_download_tool_with_pinned_revision(self, tmp_path):
        """download_tool() with revision should use it directly."""
        mock_module, mock_toolshed, mock_ts_cls = _mock_bioblend()
        mock_ts = MagicMock()
        mock_ts_cls.return_value = mock_ts
        mock_ts.repositories.get_repository_revision_install_info.return_value = [
            {"file_path": "bowtie2.xml"},
        ]
        with (
            patch.dict(sys.modules, {"bioblend": mock_module, "bioblend.toolshed": mock_toolshed}),
            patch("httpx.get") as mock_get,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<tool id='test'/>"
            mock_get.return_value = mock_resp
            dest, rev = download_tool(
                "devteam", "bowtie2",
                revision="def456",
                dest_dir=tmp_path,
            )
        assert dest.exists()
        assert rev == "def456"
        # Should not call show_repository when revision is given
        mock_ts.repositories.show_repository.assert_not_called()


class TestParseToolToSpec:
    def test_parse_valid_tool_xml(self, tmp_path):
        """parse_tool_to_spec() should extract name, inputs, outputs from XML."""
        xml_content = """\
<tool id="bowtie2_align" name="Bowtie2" version="2.5.0">
  <description>Fast and sensitive read alignment</description>
  <requirements>
    <requirement type="docker">quay.io/biocontainers/bowtie2:2.5.0</requirement>
  </requirements>
  <command>bowtie2 -x $index -U $reads -S $output</command>
  <inputs>
    <param name="index" type="data" format="fasta" />
    <param name="reads" type="data" format="fastq" />
  </inputs>
  <outputs>
    <data name="output" format="sam" from_work_dir="output.sam" />
  </outputs>
</tool>
"""
        xml_path = tmp_path / "bowtie2.xml"
        xml_path.write_text(xml_content)

        spec = parse_tool_to_spec(tmp_path)
        assert spec.name == "bowtie2_align"
        assert spec.version == "2.5.0"
        assert spec.description == "Fast and sensitive read alignment"
        assert spec.container == "quay.io/biocontainers/bowtie2:2.5.0"
        assert "index" in spec.inputs
        assert "reads" in spec.inputs
        assert spec.inputs["index"].format == "fasta"
        assert spec.inputs["reads"].format == "fastq"
        assert "output" in spec.outputs
        assert spec.outputs["output"].format == "sam"
        assert spec.outputs["output"].pattern == "output.sam"
        assert "galaxy" in spec.executors
        # Command is no longer extracted from Cheetah — left empty for
        # submit_discovered() to set the planemo invocation.
        assert spec.command == ""

    def test_parse_no_xml_raises(self, tmp_path):
        """parse_tool_to_spec() should raise FileNotFoundError if no XML."""
        with pytest.raises(FileNotFoundError):
            parse_tool_to_spec(tmp_path)

    def test_parse_minimal_tool(self, tmp_path):
        """parse_tool_to_spec() should handle a minimal tool XML."""
        xml_content = """\
<tool id="simple" name="Simple" version="0.1">
  <command>echo hello</command>
</tool>
"""
        xml_path = tmp_path / "simple.xml"
        xml_path.write_text(xml_content)

        spec = parse_tool_to_spec(tmp_path)
        assert spec.name == "simple"
        assert spec.command == ""
        assert spec.inputs == {}
        assert spec.outputs == {}

    def test_parse_output_pattern_from_work_dir_only(self, tmp_path):
        """parse_tool_to_spec() should use from_work_dir, not label, for pattern."""
        xml_content = """\
<tool id="test" name="Test" version="1.0">
  <inputs>
    <param name="input1" type="data" format="txt" />
  </inputs>
  <outputs>
    <data name="out1" format="txt" from_work_dir="results.txt" label="Human Readable Label" />
    <data name="out2" format="txt" label="Another Label" />
  </outputs>
</tool>
"""
        xml_path = tmp_path / "test.xml"
        xml_path.write_text(xml_content)

        spec = parse_tool_to_spec(tmp_path)
        assert spec.outputs["out1"].pattern == "results.txt"
        # out2 has no from_work_dir — pattern should be empty, not the label
        assert spec.outputs["out2"].pattern == ""
