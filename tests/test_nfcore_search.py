"""Tests for the nf-core modules search module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.core.engines import nfcore_search as nf
from bioledger.core.engines.nfcore_search import (
    NFCoreResult,
    _fetch_description,
    _fetch_module_subdirs,
    _github_headers,
    _list_dir,
    _search_subdirs_via_contents,
    _strip_braces,
    _val_default,
    download_module,
    generate_nfcore_wrapper,
    install_nfcore_module,
    parse_meta_yml,
    search_nfcore,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nfcore"


def _tree_payload(*subdirs: str) -> dict:
    return {
        "tree": [
            {"path": f"modules/nf-core/{s}/meta.yml", "type": "blob"}
            for s in subdirs
        ]
    }


class TestSearchNFCore:
    def test_search_returns_results(self, tmp_path):
        """search_nfcore() uses the git Trees API and matches subdirs."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload(
                "bowtie2/align", "bowtie2/build", "fastqc", "gatk4/haplotypecaller",
            )),
            # _fetch_description("bowtie2/align")
            MagicMock(status_code=200, text="description: Align reads\n"),
            # _fetch_description("bowtie2/build")
            MagicMock(status_code=200, text="description: Build index\n"),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
            results = search_nfcore("bowtie2")
        assert len(results) == 2
        assert all(isinstance(r, NFCoreResult) for r in results)
        by_subdir = {r.subdirectory: r for r in results}
        assert set(by_subdir) == {"bowtie2/align", "bowtie2/build"}
        assert by_subdir["bowtie2/align"].name == "align"
        assert by_subdir["bowtie2/align"].description == "Align reads"

    def test_search_flat_and_nested_layouts(self, tmp_path):
        """Both flat (fastqc) and nested (bowtie2/align) dirs are modules."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload(
                "fastqc", "bowtie2/align",
            )),
            MagicMock(status_code=200, text="description: QC\n"),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
            results = search_nfcore("fastqc")
        assert [r.subdirectory for r in results] == ["fastqc"]

    def test_search_no_match(self, tmp_path):
        """search_nfcore() with no matches returns empty list."""
        responses = [
            MagicMock(status_code=200, json=lambda: _tree_payload("bwa/mem")),
        ]
        with (
            patch("bioledger.core.engines.nfcore_search._CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=responses),
        ):
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


class TestParseMetaYml:
    def test_parses_real_bowtie2_meta(self):
        """parse_meta_yml handles the real nested-channel meta.yml schema."""
        meta = parse_meta_yml(FIXTURES / "bowtie2_align")
        assert meta["process_name"] == "BOWTIE2_ALIGN"
        assert meta["inputs"] == ["reads", "index", "fasta"]
        # Map channels must be detected by type (meta, meta2, meta3), not name
        kinds = [(c["name"], c["kind"], c["has_meta"])
                 for c in meta["input_channels"]]
        assert kinds == [
            ("reads", "file", True),
            ("index", "file", True),
            ("fasta", "file", True),
            ("save_unaligned", "val", False),
            ("sort_bam", "val", False),
        ]
        assert meta["parameters"] == {
            "save_unaligned": False, "sort_bam": False,
        }
        assert meta["outputs"]["bam"] == "*.bam"
        assert "versions_bowtie2" not in meta["outputs"]
        # Structured containers.docker["linux/amd64"].name
        assert meta["container"].startswith(
            "community.wave.seqera.io/library/bowtie2"
        )

    def test_parses_real_fastqc_meta(self):
        """Brace patterns prefer the clean emit key; flat layout works."""
        meta = parse_meta_yml(FIXTURES / "fastqc")
        assert meta["process_name"] == "FASTQC"
        assert meta["inputs"] == ["reads"]
        # "*_{fastqc.html}" brace pattern — dict key "*.html" wins
        assert meta["outputs"] == {"html": "*.html", "zip": "*.zip"}
        assert "fastqc:" in meta["container"]


class TestGenerateNfcoreWrapper:
    def test_wrapper_includes_module_and_channels(self, tmp_path):
        """generate_nfcore_wrapper() should produce a valid Nextflow wrapper."""
        meta = {
            "inputs": ["reads", "index"],
            "outputs": {"bam": "*.bam"},
            "container": "quay.io/biocontainers/bowtie2:2.5.0",
            "process_name": "BOWTIE2_ALIGN",
        }
        wrapper, config = generate_nfcore_wrapper(
            "bowtie2/align",
            tmp_path,
            meta,
            "test_entry_001",
        )
        assert "include" in wrapper
        assert "BOWTIE2_ALIGN" in wrapper
        assert "reads_ch" in wrapper
        assert "index_ch" in wrapper
        assert "workflow {" in wrapper
        # publishDir is a process directive — it must live in the
        # companion config via withName, not in the top-level script
        assert "publishDir" not in wrapper
        assert "withName: 'BOWTIE2_ALIGN'" in config
        assert "publishDir = [path: params.outdir, mode: 'copy']" in config

    def test_wrapper_val_params_go_to_config_not_script(self, tmp_path):
        """val channel defaults must be config params (CLI-overridable)."""
        meta = parse_meta_yml(FIXTURES / "bowtie2_align")
        wrapper, config = generate_nfcore_wrapper(
            "bowtie2/align", tmp_path, meta, "e1",
        )
        # Script passes params positionally; defaults in config so that
        # `nextflow run --save_unaligned true` actually overrides them.
        assert "params.save_unaligned = " not in wrapper
        assert "params.save_unaligned" in wrapper  # positional call arg
        assert "sort_bam = false" in config
        # 5-arg call matches the module's real input block
        assert (
            "BOWTIE2_ALIGN(reads_ch, index_ch, fasta_ch, "
            "params.save_unaligned, params.sort_bam)" in wrapper
        )

    def test_wrapper_no_inputs_uses_fallback_channel(self, tmp_path):
        """generate_nfcore_wrapper() with no inputs should use input_ch."""
        meta = {
            "inputs": [],
            "outputs": {"out": "*.out"},
            "container": "",
            "process_name": "SOMETOOL",
        }
        wrapper, config = generate_nfcore_wrapper(
            "utils/sometool",
            tmp_path,
            meta,
            "test_entry_002",
        )
        assert "input_ch" in wrapper
        assert "SOMETOOL" in wrapper
        assert config  # outputs declared → publishDir config emitted

    def test_wrapper_no_outputs_emits_empty_config(self, tmp_path):
        """Modules with no outputs and no val params need no config."""
        meta = {
            "inputs": ["reads"],
            "outputs": {},
            "container": "",
            "process_name": "SOMETOOL",
        }
        wrapper, config = generate_nfcore_wrapper(
            "utils/sometool",
            tmp_path,
            meta,
            "test_entry_003",
        )
        assert "SOMETOOL" in wrapper
        assert config == ""


class TestGithubHeaders:
    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _github_headers() == {"Accept": "application/vnd.github+json"}

    def test_with_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok123")
        assert _github_headers()["Authorization"] == "Bearer tok123"


class TestFetchModuleSubdirs:
    def test_parses_tree_and_writes_cache(self, tmp_path):
        resp = MagicMock(
            status_code=200, json=lambda: _tree_payload("fastqc", "bwa/mem")
        )
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp) as get,
        ):
            assert _fetch_module_subdirs() == ["bwa/mem", "fastqc"]
        assert (tmp_path / "tree_master.json").exists()

    def test_serves_fresh_cache_without_http(self, tmp_path):
        (tmp_path / "tree_master.json").write_text(
            '{"subdirs": ["cached/tool"]}'
        )
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=AssertionError("no http")),
        ):
            assert _fetch_module_subdirs() == ["cached/tool"]

    def test_expired_cache_refetches(self, tmp_path):
        f = tmp_path / "tree_master.json"
        f.write_text('{"subdirs": ["old/tool"]}')
        import os, time

        os.utime(f, (0, time.time() - 7200))
        resp = MagicMock(status_code=200, json=lambda: _tree_payload("new"))
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            assert _fetch_module_subdirs() == ["new"]

    def test_corrupt_cache_refetches(self, tmp_path):
        (tmp_path / "tree_master.json").write_text("{not json")
        resp = MagicMock(status_code=200, json=lambda: _tree_payload("x"))
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            assert _fetch_module_subdirs() == ["x"]

    def test_non_200_returns_empty(self, tmp_path):
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=MagicMock(status_code=403)),
        ):
            assert _fetch_module_subdirs() == []

    def test_malformed_payload_returns_empty(self, tmp_path):
        resp = MagicMock(status_code=200, json=lambda: {"unexpected": 1})
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            assert _fetch_module_subdirs() == []

    def test_exception_returns_empty(self, tmp_path):
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", side_effect=OSError("net down")),
        ):
            assert _fetch_module_subdirs() == []


class TestListDir:
    def test_returns_list_payload(self):
        payload = [{"name": "a"}, {"name": "b"}]
        with patch("httpx.get", return_value=MagicMock(
            status_code=200, json=lambda: payload
        )):
            assert _list_dir("http://x") == payload

    def test_non_200_empty(self):
        with patch("httpx.get", return_value=MagicMock(status_code=404)):
            assert _list_dir("http://x") == []

    def test_non_list_payload_empty(self):
        with patch("httpx.get", return_value=MagicMock(
            status_code=200, json=lambda: {"dict": "not list"}
        )):
            assert _list_dir("http://x") == []


class TestSearchSubdirsViaContents:
    def test_flat_and_nested_detection(self):
        listing = {
            "modules/nf-core": [
                {"name": "fastqc", "type": "dir"},
                {"name": "bowtie2", "type": "dir"},
                {"name": "readme.md", "type": "file"},
            ],
            "modules/nf-core/fastqc": [{"name": "meta.yml"}],
            "modules/nf-core/bowtie2": [
                {"name": "align", "type": "dir"},
                {"name": "test", "type": "dir"},
            ],
            "modules/nf-core/bowtie2/align": [{"name": "meta.yml"}],
        }

        def fake_list(url):
            return listing.get(url.split("contents/")[-1], [])

        with patch.object(nf, "_list_dir", side_effect=fake_list):
            subdirs = _search_subdirs_via_contents()
        # flat module + nested module; "test" dir ignored; files ignored
        assert subdirs == ["bowtie2/align", "fastqc"]

    def test_search_falls_back_when_tree_empty(self, tmp_path):
        with (
            patch.object(nf, "_fetch_module_subdirs", return_value=[]),
            patch.object(
                nf, "_search_subdirs_via_contents",
                return_value=["fastqc"],
            ),
            patch.object(nf, "_fetch_description", return_value="d"),
        ):
            results = search_nfcore("fastqc")
        assert [r.subdirectory for r in results] == ["fastqc"]


class TestFetchDescription:
    def test_non_200_empty(self):
        with patch("httpx.get", return_value=MagicMock(status_code=404)):
            assert _fetch_description("x") == ""

    def test_yaml_description(self):
        resp = MagicMock(status_code=200, text="description:  Trim reads \n")
        with patch("httpx.get", return_value=resp):
            assert _fetch_description("x") == "Trim reads"

    def test_non_string_description_empty(self):
        resp = MagicMock(status_code=200, text="description: [a, b]\n")
        with patch("httpx.get", return_value=resp):
            assert _fetch_description("x") == ""

    def test_malformed_yaml_line_scan_fallback(self):
        # YAML that fails safe_load but has a description: line
        resp = MagicMock(
            status_code=200, text="description: {unclosed\n"
        )
        with (
            patch("httpx.get", return_value=resp),
            patch("yaml.safe_load", side_effect=ValueError("bad")),
        ):
            assert _fetch_description("x") == "{unclosed"

    def test_http_exception_empty(self):
        with patch("httpx.get", side_effect=OSError("down")):
            assert _fetch_description("x") == ""


class TestDownloadModuleErrors:
    def test_non_200_skips_file(self, tmp_path):
        with patch("httpx.get", return_value=MagicMock(status_code=404)):
            dest = download_module("a/b", dest_dir=tmp_path)
        assert dest.exists()
        assert not (dest / "main.nf").exists()

    def test_exception_warns_and_continues(self, tmp_path):
        with patch("httpx.get", side_effect=OSError("down")):
            dest = download_module("a/b", dest_dir=tmp_path)
        assert dest.exists()

    def test_uses_cache_dir_when_no_dest(self, tmp_path):
        resp = MagicMock(status_code=200, text="x")
        with (
            patch.object(nf, "_CACHE_DIR", tmp_path),
            patch("httpx.get", return_value=resp),
        ):
            dest = download_module("bowtie2/align")
        assert dest == tmp_path / "bowtie2_align"
        assert (dest / "main.nf").exists()


class _Proc:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _modules_json(subdir, sha):
    import json as j

    return j.dumps(
        {
            "repos": {
                "https://github.com/nf-core/modules.git": {
                    "modules": {"nf-core": {subdir: {"git_sha": sha}}}
                }
            }
        }
    )


class TestInstallNfcoreModule:
    def test_install_success_with_modules_json(self, tmp_path):
        dest = tmp_path / "mod"
        module_dir = dest / "modules" / "nf-core" / "bowtie2" / "align"

        def fake_run(cmd, **kw):
            module_dir.mkdir(parents=True)
            (dest / "modules.json").write_text(
                _modules_json("bowtie2/align", "abc123")
            )
            return _Proc(0)

        with (
            patch("subprocess.run", side_effect=fake_run) as run,
            patch("shutil.which", return_value="/bin/nf-core"),
        ):
            mdir, sha = install_nfcore_module("bowtie2/align", dest)

        assert mdir == module_dir
        assert sha == "abc123"
        assert (dest / ".nf-core.yml").exists()
        cmd = run.call_args[0][0]
        assert cmd[:3] == ["/bin/nf-core", "modules", "install"]
        assert "--sha" not in cmd

    def test_install_with_revision_adds_sha(self, tmp_path):
        dest = tmp_path / "mod"

        def fake_run(cmd, **kw):
            (dest / "modules" / "nf-core" / "x").mkdir(parents=True)
            return _Proc(0)

        with (
            patch("subprocess.run", side_effect=fake_run) as run,
            patch("shutil.which", return_value=None),
            patch("httpx.get", return_value=MagicMock(
                status_code=200, json=lambda: {"sha": "deadbeef"}
            )),
        ):
            install_nfcore_module("x", dest, revision="v1.2")
        assert run.call_args[0][0][-2:] == ["--sha", "v1.2"]

    def test_install_failure_raises(self, tmp_path):
        with (
            patch("subprocess.run", return_value=_Proc(1, stderr="boom")),
            patch("shutil.which", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                install_nfcore_module("x", tmp_path / "d")

    def test_sha_walk_fallback_for_odd_layout(self, tmp_path):
        dest = tmp_path / "mod"
        module_dir = dest / "modules" / "nf-core" / "t"

        def fake_run(cmd, **kw):
            module_dir.mkdir(parents=True)
            import json as j

            (dest / "modules.json").write_text(
                j.dumps(
                    {"repos": {"weird": {"nested": {"t": {"git_sha": "w1"}}}}}
                )
            )
            return _Proc(0)

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("shutil.which", return_value=None),
        ):
            _, sha = install_nfcore_module("t", dest)
        assert sha == "w1"

    def test_no_modules_json_uses_commit_api(self, tmp_path):
        dest = tmp_path / "mod"

        def fake_run(cmd, **kw):
            (dest / "modules" / "nf-core" / "t").mkdir(parents=True)
            return _Proc(0)

        resp = MagicMock(status_code=200, json=lambda: {"sha": "api_sha"})
        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("shutil.which", return_value=None),
            patch("httpx.get", return_value=resp),
        ):
            _, sha = install_nfcore_module("t", dest)
        assert sha == "api_sha"

    def test_module_dir_rglob_fallback(self, tmp_path):
        dest = tmp_path / "mod"

        def fake_run(cmd, **kw):
            weird = dest / "somewhere" / "mytool"
            weird.mkdir(parents=True)
            (weird / "main.nf").write_text("process MYTOOL {}")
            return _Proc(0)

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("shutil.which", return_value=None),
            patch("httpx.get", side_effect=OSError),
        ):
            mdir, sha = install_nfcore_module("mytool", dest)
        assert (mdir / "main.nf").exists()
        assert sha == ""


class TestHelpers:
    def test_val_defaults(self):
        assert _val_default("boolean") is False
        assert _val_default("integer") == 0
        assert _val_default("float") == 0.0
        assert _val_default("string") == ""
        assert _val_default("unknown") == ""

    def test_strip_braces(self):
        assert _strip_braces("*.{fa,fasta,fna}") == "*.fa"
        assert _strip_braces("*_{fastqc.html}") == "*_fastqc.html"
        assert _strip_braces("plain.txt") == "plain.txt"

    def test_strip_braces_in_middle(self):
        assert _strip_braces("pre{a,b}post") == "preapost"


class TestParseMetaYmlEdges:
    def _write(self, d, meta_text=None, nf_text=None):
        d.mkdir(parents=True, exist_ok=True)
        if meta_text is not None:
            (d / "meta.yml").write_text(meta_text)
        if nf_text is not None:
            (d / "main.nf").write_text(nf_text)
        return d

    def test_missing_meta_yml(self, tmp_path):
        meta = parse_meta_yml(self._write(tmp_path / "m"))
        assert meta["inputs"] == []
        assert meta["outputs"] == {}
        assert meta["container"] == ""

    def test_malformed_meta_yml(self, tmp_path):
        d = self._write(tmp_path / "m", meta_text="{bad: [unclosed")
        meta = parse_meta_yml(d)
        assert meta["inputs"] == []

    def test_container_from_main_nf(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text="input: []\n",
            nf_text=(
                "process T {\n"
                "  container 'quay.io/biocontainers/t:1.0'\n"
                "}\n"
            ),
        )
        assert parse_meta_yml(d)["container"] == (
            "quay.io/biocontainers/t:1.0"
        )

    def test_container_skips_dynamic_and_prefers_depot(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            nf_text=(
                "process T {\n"
                "  container \"${ workflow.containerEngine == 'singularity'"
                " ? 'a' : 'b' }\"\n"
                "  container 'other.io/t:1'\n"
                "  container 'depot.galaxyproject.org/t:2'\n"
                "}\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["container"] == "depot.galaxyproject.org/t:2"

    def test_process_name_from_main_nf(self, tmp_path):
        d = self._write(
            tmp_path / "m", nf_text="process MY_PROC {\n}\n"
        )
        assert parse_meta_yml(d)["process_name"] == "MY_PROC"

    def test_process_name_falls_back_to_meta_name(self, tmp_path):
        d = self._write(tmp_path / "m", meta_text="name: mytool\n")
        assert parse_meta_yml(d)["process_name"] == "MYTOOL"

    def test_legacy_flat_input_shape(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text="input:\n  - name: reads\n    type: file\n",
        )
        meta = parse_meta_yml(d)
        assert meta["inputs"] == ["reads"]

    def test_val_types(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "input:\n"
                "  - flag:\n      type: boolean\n"
                "  - count:\n      type: integer\n"
                "  - ratio:\n      type: float\n"
                "  - label:\n      type: string\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["parameters"] == {
            "flag": False, "count": 0, "ratio": 0.0, "label": "",
        }

    def test_outputs_string_channel_and_versions_skipped(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "output:\n"
                "  bam: '*.bam'\n"
                "  versions_x: 'v.yml'\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["outputs"] == {"bam": "*.bam"}

    def test_outputs_list_shape(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "output:\n"
                "  - html:\n"
                "      - - '*.html':\n"
                "            type: file\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["outputs"] == {"html": "*.html"}

    def test_legacy_flat_output_shape(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "output:\n"
                "  - name: bam\n"
                "    type: file\n"
                "    pattern: '*.bam'\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["outputs"]["bam"] == "*.bam"

    def test_output_brace_pattern_from_info(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "output:\n"
                "  - html:\n"
                "      - - '{x,y}':\n"
                "            type: file\n"
                "            pattern: '*.{html,htm}'\n"
            ),
        )
        meta = parse_meta_yml(d)
        assert meta["outputs"]["html"] == "*.html"

    def test_containers_structured_string(self, tmp_path):
        d = self._write(
            tmp_path / "m",
            meta_text=(
                "containers:\n"
                "  docker:\n"
                "    linux/amd64: 'img:1'\n"
            ),
        )
        assert parse_meta_yml(d)["container"] == "img:1"


class TestGenerateNfcoreWrapperEdges:
    def test_has_meta_channel_uses_tuple(self, tmp_path):
        meta = {
            "inputs": ["reads"],
            "outputs": {"bam": "*.bam"},
            "process_name": "T",
            "input_channels": [
                {"name": "reads", "kind": "file", "has_meta": True}
            ],
        }
        wrapper, _ = generate_nfcore_wrapper("t/x", tmp_path, meta, "e")
        assert "Channel.of(" in wrapper
        assert "single_end: true" in wrapper

    def test_no_process_name_falls_back_to_subdir(self, tmp_path):
        meta = {"inputs": [], "outputs": {}, "input_channels": []}
        wrapper, _ = generate_nfcore_wrapper(
            "bowtie2/align", tmp_path, meta, "e"
        )
        assert "BOWTIE2_ALIGN" in wrapper
