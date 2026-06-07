"""Tests for index generators in toolspec-schema and isatab-schema."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bioledger_isatab_schema.index import build_index as build_study_index
from bioledger_toolspec_schema.index import build_index as build_toolspec_index


class TestToolspecIndexGenerator:
    """Tests for toolspec library index generation."""

    def test_build_index_finds_nested_specs(self, tmp_path: Path):
        """build_index finds spec.yaml files in nested family/tool directories."""
        # Create nested structure: specs/samtools/samtools-faidx/spec.yaml
        specs_dir = tmp_path / "specs"
        tool_dir = specs_dir / "samtools" / "samtools-faidx"
        tool_dir.mkdir(parents=True)

        spec = {
            "execution": {
                "name": "samtools-faidx",
                "version": "1.20",
                "description": "Build FASTA index",
                "container": "samtools:latest",
                "categories": ["reference-prep"],
                "inputs": {"ref_fasta": {}},
                "outputs": {"fai": {}},
            }
        }
        (tool_dir / "spec.yaml").write_text(yaml.dump(spec))

        entries = build_toolspec_index(specs_dir)

        assert len(entries) == 1
        assert entries[0]["name"] == "samtools-faidx"
        assert entries[0]["family"] == "samtools"
        assert entries[0]["version"] == "1.20"
        assert entries[0]["path"] == "samtools/samtools-faidx"

    def test_build_index_skips_invalid_yaml(self, tmp_path: Path):
        """build_index skips files that fail to parse."""
        specs_dir = tmp_path / "specs"
        tool_dir = specs_dir / "bad"
        tool_dir.mkdir(parents=True)

        (tool_dir / "spec.yaml").write_text("invalid: yaml: [")

        entries = build_toolspec_index(specs_dir)
        assert len(entries) == 0

    def test_build_index_sorts_by_path(self, tmp_path: Path):
        """build_index returns entries sorted by path."""
        specs_dir = tmp_path / "specs"

        # Create in reverse order
        for name in ["zzz-tool", "aaa-tool", "mmm-tool"]:
            tool_dir = specs_dir / "family" / name
            tool_dir.mkdir(parents=True)
            spec = {"execution": {"name": name, "inputs": {}, "outputs": {}}}
            (tool_dir / "spec.yaml").write_text(yaml.dump(spec))

        entries = build_toolspec_index(specs_dir)
        names = [e["name"] for e in entries]
        assert names == ["aaa-tool", "mmm-tool", "zzz-tool"]


class TestIsatabIndexGenerator:
    """Tests for ISA-Tab study index generation."""

    def test_build_index_includes_isa_files(self, tmp_path: Path):
        """build_index includes list of ISA structural files."""
        studies_dir = tmp_path / "studies"
        study_dir = studies_dir / "PRJNA123"
        study_dir.mkdir(parents=True)

        # Create manifest
        manifest = {"study_type": "experimental_data", "organism": "P. falciparum"}
        (study_dir / "manifest.yaml").write_text(yaml.dump(manifest))

        # Create ISA files
        (study_dir / "i_investigation.txt").write_text("Study ID")
        (study_dir / "s_study.txt").write_text("Sample\tData")
        (study_dir / "a_assay.txt").write_text("Assay\tData")
        # Non-.txt file should not be included
        (study_dir / "data.fastq").write_bytes(b"seq")

        entries = build_study_index(studies_dir)

        assert len(entries) == 1
        entry = entries[0]
        assert entry["accession"] == "PRJNA123"
        assert entry["study_type"] == "experimental_data"
        assert "isa_files" in entry
        assert sorted(entry["isa_files"]) == ["a_assay.txt", "i_investigation.txt", "s_study.txt"]

    def test_build_index_extracts_title_from_investigation(self, tmp_path: Path):
        """build_index extracts title/description from i_investigation.txt."""
        studies_dir = tmp_path / "studies"
        study_dir = studies_dir / "PRJNA456"
        study_dir.mkdir(parents=True)

        manifest = {"study_type": "reference_genome"}
        (study_dir / "manifest.yaml").write_text(yaml.dump(manifest))

        investigation = (
            "Study Title\tMy Study Title\n"
            "Study Description\tMy study description\n"
        )
        (study_dir / "i_investigation.txt").write_text(investigation)

        entries = build_study_index(studies_dir)

        assert entries[0]["title"] == "My Study Title"
        assert entries[0]["description"] == "My study description"

    def test_build_index_counts_manifest_files(self, tmp_path: Path):
        """build_index counts files declared in manifest."""
        studies_dir = tmp_path / "studies"
        study_dir = studies_dir / "PRJNA789"
        study_dir.mkdir(parents=True)

        manifest = {
            "study_type": "experimental_data",
            "files": [
                {"filename": "file1.fastq", "format": "fastq"},
                {"filename": "file2.fastq", "format": "fastq"},
            ]
        }
        (study_dir / "manifest.yaml").write_text(yaml.dump(manifest))
        (study_dir / "i_investigation.txt").write_text("Study")

        entries = build_study_index(studies_dir)

        assert entries[0]["file_count"] == 2
        assert "fastq" in entries[0]["formats"]
