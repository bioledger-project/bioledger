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


def _tree_payload(*paths: str) -> dict:
    return {"tree": [{"path": p, "type": "blob"} for p in paths]}


class TestSearchWrappers:
    def test_search_returns_results(self, tmp_path):
        """search_wrappers() uses the git Trees API; dirs with wrapper.py."""
        tree = _tree_payload(
            "bio/samtools/sort/wrapper.py",
            "bio/samtools/sort/meta.yaml",
            "bio/samtools/view/wrapper.py",
            "bio/bgzip/test/Snakefile",  # not a wrapper — no wrapper.py
            "docs/index.md",
        )
        resp = MagicMock(status_code=200, json=lambda: tree)
        with (
            patch("bioledger.core.engines.snakemake_search._CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            results = search_wrappers("samtools")
        assert sorted(r.path for r in results) == [
            "bio/samtools/sort", "bio/samtools/view",
        ]
        assert all(isinstance(r, SnakemakeWrapperResult) for r in results)

    def test_search_no_matches(self, tmp_path):
        """search_wrappers() should return empty list for no matches."""
        tree = _tree_payload("bio/samtools/sort/wrapper.py")
        resp = MagicMock(status_code=200, json=lambda: tree)
        with (
            patch("bioledger.core.engines.snakemake_search._CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            results = search_wrappers("nonexistent_tool")
        assert results == []


class TestDownloadWrapper:
    def test_download_wrapper_creates_files(self, tmp_path):
        """download_wrapper() resolves master→SHA and downloads files."""
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "commits/master" in url:
                resp.json.return_value = {"sha": "deadbeef"}
                resp.raise_for_status = lambda: None
            elif "meta.yaml" in url:
                resp.text = "name: samtools sort\ndescription: Sort bam file"
            elif "environment.yaml" in url:
                resp.text = "channels:\n  - bioconda"
            elif "wrapper.py" in url:
                resp.text = "assert snakemake"
            elif "test/Snakefile" in url:
                resp.text = "rule samtools_sort:\n  input:\n    \"mapped/{sample}.bam\""
            else:
                resp.status_code = 404
            return resp

        with patch("httpx.get", side_effect=mock_get):
            dest, rev = download_wrapper(
                "bio/samtools/sort",
                dest_dir=tmp_path,
            )
        assert dest.exists()
        assert (dest / "meta.yaml").exists()
        assert (dest / "test_Snakefile").exists()
        assert rev == "deadbeef"

    def test_download_wrapper_with_pinned_revision(self, tmp_path):
        """download_wrapper() resolves a pinned tag/branch to a SHA."""
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "commits/v7.1.0" in url:
                resp.json.return_value = {"sha": "cafe" * 10}
                resp.raise_for_status = lambda: None
            else:
                resp.text = "x: 1"
            return resp

        with patch("httpx.get", side_effect=mock_get):
            dest, rev = download_wrapper(
                "bio/samtools/sort",
                revision="v7.1.0",
                dest_dir=tmp_path,
            )
        assert rev == "cafe" * 10
        # Files fetched under the resolved SHA
        assert (dest / "test_Snakefile").exists()

    def test_download_wrapper_404_on_snakefile_raises(self, tmp_path):
        """A missing test/Snakefile must raise, not produce an empty dir."""
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "commits/" in url:
                resp.json.return_value = {"sha": "deadbeef"}
                resp.raise_for_status = lambda: None
            elif "test/Snakefile" in url:
                resp.status_code = 404
            else:
                resp.text = "x: 1"
            return resp

        with (
            patch("httpx.get", side_effect=mock_get),
            pytest.raises(RuntimeError, match="404"),
        ):
            download_wrapper("bio/bogus/tool", dest_dir=tmp_path)


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


FIXTURES = Path(__file__).parent / "fixtures" / "snakemake"


class TestParseRealFixtures:
    """Parse real snakemake-wrappers test/Snakefiles (keyword syntax,
    multi-line calls, multi-rule files with use-rule variants)."""

    def test_bowtie2_align_fixture(self):
        spec = parse_wrapper_to_spec(
            FIXTURES / "bowtie2_align", "bio/bowtie2/align",
        )
        # First rule containing wrapper: is selected (file has 4 rules +
        # a `use rule ... with:` variant sharing the same interface)
        assert spec.inputs["sample"].collection is True
        assert "idx" in spec.inputs
        assert spec.inputs["idx"].collection is False
        # multiext(...) is one named input, not six bogus positionals
        assert set(spec.inputs) == {"sample", "idx"}
        assert "extra" in spec.parameters
        # threads >= 2 required by this wrapper
        assert spec.parameters["threads"].default == 8
        # {sample} wildcards concretized for standalone runs
        assert all("{" not in o.pattern for o in spec.outputs.values())

    def test_samtools_sort_fixture(self):
        spec = parse_wrapper_to_spec(
            FIXTURES / "samtools_sort", "bio/samtools/sort",
        )
        assert set(spec.inputs) == {"input_0"}
        assert spec.parameters["mem"].default == "64G"
        assert spec.parameters["extra"].default == ""
        assert spec.parameters["threads"].default == 8
        assert spec.outputs["output_0"].pattern == "mapped/out.sorted.bam"

    def test_generated_snakefile_threads_and_list_inputs(self):
        """threads is a directive (>=2, config-overridable); lists stay lists."""
        spec = parse_wrapper_to_spec(
            FIXTURES / "bowtie2_align", "bio/bowtie2/align",
        )
        sf = generate_wrapper_snakefile("bio/bowtie2/align", spec, "abc123")
        assert "threads: max(2, config.get('threads', 8))" in sf
        assert "sample=config.get('sample', [])" in sf
        # threads must NOT leak into the params: block
        params_idx = sf.index("params:")
        threads_idx = sf.index("threads:")
        assert "threads=config" not in sf[params_idx:threads_idx]


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
