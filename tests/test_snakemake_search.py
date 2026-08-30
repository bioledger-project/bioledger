"""Tests for the Snakemake wrapper search module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines.snakemake_search import (
    SnakemakeWrapperResult,
    download_wrapper,
    generate_wrapper_snakefile,
    parse_wrapper_to_spec,
    search_wrappers,
)


class TestSearchWrappers:
    def test_search_returns_results(self):
        """search_wrappers() should return SnakemakeWrapperResult objects."""
        # Mock: top-level dirs (bio, utils), then bio/samtools has subdirs (sort)
        responses = [
            # 1. List top-level dirs
            [{"name": "bio", "type": "dir"}, {"name": "utils", "type": "dir"}],
            # 2. List bio/ contents
            [{"name": "samtools", "type": "dir"}],
            # 3. List bio/samtools/ contents — subdirs = individual wrappers
            [{"name": "sort", "type": "dir"}],
            # 4. List utils/ contents — no subdirs, the dir itself is a wrapper
            [{"name": "someutil", "type": "dir"}],
            # 5. List bio/samtools/sort/ contents — has subdirs
            [{"name": "wrapper.py", "type": "file"}],
            # 6. List utils/someutil/ contents — no subdirs
            [{"name": "wrapper.py", "type": "file"}],
        ]
        mock_responses = [MagicMock(status_code=200, json=lambda r=r: r) for r in responses]
        with patch("httpx.get", side_effect=mock_responses):
            results = search_wrappers("samtools")
        assert len(results) == 1
        assert results[0].name == "sort"
        assert results[0].path == "bio/samtools/sort"

    def test_search_no_matches(self):
        """search_wrappers() should return empty list for no matches."""
        responses = [
            [{"name": "bio", "type": "dir"}],
            [{"name": "samtools", "type": "dir"}],
            [{"name": "sort", "type": "dir"}],
            [{"name": "wrapper.py", "type": "file"}],
        ]
        mock_responses = [MagicMock(status_code=200, json=lambda r=r: r) for r in responses]
        with patch("httpx.get", side_effect=mock_responses):
            results = search_wrappers("nonexistent_tool")
        assert results == []


class TestDownloadWrapper:
    def test_download_wrapper_creates_files(self, tmp_path):
        """download_wrapper() should download meta.yaml and test/Snakefile."""
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "meta.yaml" in url:
                resp.text = "name: samtools sort\ndescription: Sort bam file"
            elif "environment.yaml" in url:
                resp.text = "channels:\n  - bioconda"
            elif "test/Snakefile" in url:
                resp.text = "rule samtools_sort:\n  input:\n    \"mapped/{sample}.bam\""
            elif "commits/master" in url:
                resp.json.return_value = {"sha": "deadbeef"}
                resp.text = "{}"
            else:
                resp.status_code = 404
            return resp

        with (
            patch("httpx.get", side_effect=mock_get),
        ):
            dest, rev = download_wrapper(
                "bio/samtools/sort",
                dest_dir=tmp_path,
            )
        assert dest.exists()
        assert (dest / "meta.yaml").exists()
        assert (dest / "test_Snakefile").exists()
        assert rev == "deadbeef"

    def test_download_wrapper_with_pinned_revision(self, tmp_path):
        """download_wrapper() with revision should use it as the ref."""
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "name: test"
            return resp

        with patch("httpx.get", side_effect=mock_get):
            dest, rev = download_wrapper(
                "bio/samtools/sort",
                revision="abc123",
                dest_dir=tmp_path,
            )
        assert rev == "abc123"


class TestParseWrapperToSpec:
    def test_parse_valid_wrapper(self, tmp_path):
        """parse_wrapper_to_spec() should extract inputs/outputs from test/Snakefile."""
        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        (wrapper_dir / "meta.yaml").write_text(
            "name: samtools sort\n"
            "description: Sort bam file with samtools.\n"
        )
        (wrapper_dir / "test_Snakefile").write_text(
            'rule samtools_sort:\n'
            '    input:\n'
            '        "mapped/{sample}.bam",\n'
            '    output:\n'
            '        "mapped/{sample}.sorted.bam",\n'
            '    params:\n'
            '        mem="64G",\n'
            '        extra="",\n'
            '    threads: 8\n'
            '    wrapper:\n'
            '        "master/bio/samtools/sort"\n'
        )

        spec = parse_wrapper_to_spec(wrapper_dir, "bio/samtools/sort")
        assert spec.name == "bio_samtools_sort"
        assert spec.description == "Sort bam file with samtools."
        assert "snakemake" in spec.executors
        assert spec.command == ""
        assert len(spec.inputs) >= 1
        assert len(spec.outputs) >= 1
        assert "mem" in spec.parameters
        assert "extra" in spec.parameters

    def test_parse_params_with_inline_comments(self, tmp_path):
        """Real-world wrappers annotate params with inline comments — must be stripped.

        Regression test for a bug where comments were not stripped before
        quote-trimming, producing garbage defaults like
        '64G", # choose as required, the more the better'.
        """
        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        (wrapper_dir / "test_Snakefile").write_text(
            'rule samtools_sort:\n'
            '    input:\n'
            '        "mapped/{sample}.bam",\n'
            '    output:\n'
            '        "mapped/{sample}.sorted.bam",\n'
            '    log:\n'
            '        "logs/{sample}.log",\n'
            '    params:\n'
            '        mem="64G", # choose as required, the more the better\n'
            '        extra="",\n'
            '    threads: 8\n'
            '    wrapper:\n'
            '        "master/bio/samtools/sort"\n'
        )

        spec = parse_wrapper_to_spec(wrapper_dir, "bio/samtools/sort")
        assert spec.parameters["mem"].default == "64G"
        assert spec.parameters["extra"].default == ""

    def test_parse_no_meta_yaml(self, tmp_path):
        """parse_wrapper_to_spec() should handle missing meta.yaml."""
        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        (wrapper_dir / "test_Snakefile").write_text(
            'rule test_rule:\n'
            '    input:\n'
            '        "input.txt",\n'
            '    output:\n'
            '        "output.txt",\n'
        )

        spec = parse_wrapper_to_spec(wrapper_dir, "bio/test")
        assert spec.name == "bio_test"
        assert spec.description == ""
        assert len(spec.inputs) >= 1
        assert len(spec.outputs) >= 1

    def test_parse_no_snakefile(self, tmp_path):
        """parse_wrapper_to_spec() should handle missing test/Snakefile."""
        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        (wrapper_dir / "meta.yaml").write_text("description: Test tool\n")

        spec = parse_wrapper_to_spec(wrapper_dir, "bio/test")
        assert spec.name == "bio_test"
        assert spec.inputs == {}
        assert spec.outputs == {}


class TestGenerateWrapperSnakefile:
    def test_generates_valid_snakefile(self):
        """generate_wrapper_snakefile() should produce a Snakefile with wrapper: directive."""
        from bioledger.toolspec.models import (
            ExecutionSpec,
            ToolInput,
            ToolOutput,
            ToolParameter,
            ParamType,
        )

        spec = ExecutionSpec(
            name="samtools_sort",
            command="",
            executors=["snakemake"],
            inputs={"bam": ToolInput(name="bam", collection=False, format="bam")},
            outputs={
                "sorted_bam": ToolOutput(
                    name="sorted_bam", pattern="*.sorted.bam", format="bam"
                )
            },
            parameters={
                "extra": ToolParameter(
                    name="extra", type=ParamType.STRING, default="", description=""
                )
            },
        )

        snakefile = generate_wrapper_snakefile(
            "bio/samtools/sort", spec, revision="master"
        )
        assert "rule samtools_sort:" in snakefile
        assert "input:" in snakefile
        assert "bam=config['bam']" in snakefile
        assert "output:" in snakefile
        assert "*.sorted.bam" in snakefile
        assert "params:" in snakefile
        assert "extra=config.get('extra', '')" in snakefile
        assert "wrapper:" in snakefile
        assert "master/bio/samtools/sort" in snakefile

    def test_string_default_is_quoted(self):
        """Non-empty string param defaults must be quoted (repr), not bare words."""
        from bioledger.toolspec.models import ExecutionSpec, ToolParameter, ParamType

        spec = ExecutionSpec(
            name="samtools_sort",
            command="",
            executors=["snakemake"],
            parameters={
                "mem": ToolParameter(
                    name="mem", type=ParamType.STRING, default="64G", description=""
                )
            },
        )

        snakefile = generate_wrapper_snakefile(
            "bio/samtools/sort", spec, revision="master"
        )
        # Must be quoted — "mem=config.get('mem', 64G)" is invalid Python syntax
        assert "mem=config.get('mem', '64G')" in snakefile
        assert "config.get('mem', 64G)" not in snakefile

    def test_no_inputs_outputs(self):
        """generate_wrapper_snakefile() should handle empty inputs/outputs."""
        from bioledger.toolspec.models import ExecutionSpec

        spec = ExecutionSpec(
            name="simple_tool",
            command="",
            executors=["snakemake"],
        )

        snakefile = generate_wrapper_snakefile(
            "bio/simple", spec, revision="v1.0"
        )
        assert "rule simple_tool:" in snakefile
        assert "input:" in snakefile
        assert "output:" in snakefile
        assert "wrapper:" in snakefile
        assert "v1.0/bio/simple" in snakefile

    def test_pinned_revision_in_wrapper_directive(self):
        """generate_wrapper_snakefile() should embed the revision in the wrapper path."""
        from bioledger.toolspec.models import ExecutionSpec

        spec = ExecutionSpec(
            name="test_tool",
            command="",
            executors=["snakemake"],
        )

        snakefile = generate_wrapper_snakefile(
            "bio/test", spec, revision="abc123def"
        )
        assert "abc123def/bio/test" in snakefile
