"""Tests for bioledger CLI commands (session/tool/library/study/config).

All filesystem state is isolated by pointing HOME at tmp_path; remote calls
(search backends, library index fetches, study downloads, crate building)
are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from bioledger.apps.cli.main import app
from bioledger.ledger.models import (
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.ledger.store import LedgerStore

runner = CliRunner()


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    """Redirect Path.home() (used by LedgerStore/ToolStore/BioLedgerConfig)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(iso_home):
    return LedgerStore()


def _session(store: LedgerStore, name: str = "", **kw) -> LedgerSession:
    s = LedgerSession(name=name, **kw)
    store.create_session(s)
    return s


def _entry(**kw) -> LedgerEntry:
    kw.setdefault("kind", EntryKind.TOOL_RUN)
    kw.setdefault("tool_spec_name", "t")
    kw.setdefault("exit_code", 0)
    return LedgerEntry(**kw)


# ---------------------------------------------------------------------------
# session commands
# ---------------------------------------------------------------------------


class TestSessionNew:
    def test_creates_unnamed_session(self, iso_home):
        result = runner.invoke(app, ["session", "new"])
        assert result.exit_code == 0
        assert "created" in result.output
        sessions = LedgerStore().list_sessions()
        assert len(sessions) == 1

    def test_creates_named_session(self, iso_home):
        result = runner.invoke(
            app, ["session", "new", "--name", "rnaseq", "--description", "d"]
        )
        assert result.exit_code == 0
        assert "rnaseq" in result.output

    def test_duplicate_name_fails(self, iso_home):
        _session(LedgerStore(), name="dup")
        result = runner.invoke(app, ["session", "new", "--name", "dup"])
        assert result.exit_code == 1
        assert "already in use" in result.output


class TestSessionList:
    def test_empty(self, iso_home):
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        assert "No sessions found" in result.output

    def test_lists_sessions(self, store):
        _session(store, name="s1")
        _session(store, name="s2")
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        assert "s1" in result.output and "s2" in result.output

    def test_archived_hidden_unless_all(self, store):
        s = _session(store, name="arch")
        store.archive_session(s.id)
        result = runner.invoke(app, ["session", "list"])
        assert "arch" not in result.output
        result = runner.invoke(app, ["session", "list", "--all"])
        assert "arch" in result.output


class TestResolveSession:
    def test_by_id(self, store):
        s = _session(store, name="x")
        result = runner.invoke(app, ["session", "show", s.id])
        assert result.exit_code == 0
        assert s.id in result.output

    def test_by_name(self, store):
        _session(store, name="myname")
        result = runner.invoke(app, ["session", "show", "myname"])
        assert result.exit_code == 0

    def test_by_unique_prefix(self, store):
        s = _session(store)
        result = runner.invoke(app, ["session", "show", s.id[:8]])
        assert result.exit_code == 0

    def test_ambiguous_prefix_fails(self, store):
        # Two sessions whose ids share a prefix
        s1 = _session(store)
        s2 = _session(store)
        store._conn.execute(
            "UPDATE sessions SET id = ? WHERE id = ?",
            (s1.id[:4] + "z" * (len(s2.id) - 4), s2.id),
        )
        store._conn.commit()
        result = runner.invoke(app, ["session", "show", s1.id[:4]])
        assert result.exit_code == 1
        assert "ambiguous" in result.output

    def test_not_found(self, store):
        result = runner.invoke(app, ["session", "show", "nonexistent-ref"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestSessionShow:
    def test_shows_details_and_chat(self, store):
        from bioledger.ledger.models import ChatMessage

        s = _session(store, name="s", description="desc")
        store.append_message(s.id, ChatMessage(role="user", content="hello there"))
        store.append_entry(s.id, _entry())
        result = runner.invoke(app, ["session", "show", s.id])
        assert result.exit_code == 0
        assert "desc" in result.output
        assert "hello there" in result.output
        assert "Entries: 1" in result.output


class TestSessionRenameDescribeArchive:
    def test_rename(self, store):
        s = _session(store, name="old")
        result = runner.invoke(app, ["session", "rename", "old", "new"])
        assert result.exit_code == 0
        assert store.load_session(s.id).name == "new"

    def test_rename_to_existing_name_fails(self, store):
        _session(store, name="taken")
        _session(store, name="src")
        result = runner.invoke(app, ["session", "rename", "src", "taken"])
        assert result.exit_code == 1

    def test_describe(self, store):
        s = _session(store, name="d")
        result = runner.invoke(app, ["session", "describe", "d", "new desc"])
        assert result.exit_code == 0
        assert store.load_session(s.id).description == "new desc"

    def test_archive(self, store):
        s = _session(store, name="a")
        result = runner.invoke(app, ["session", "archive", "a"])
        assert result.exit_code == 0
        assert "archived" in result.output
        assert store.load_session(s.id).status.value == "archived"


# ---------------------------------------------------------------------------
# tool commands
# ---------------------------------------------------------------------------

_SPEC = {
    "spec_version": "0.1",
    "execution": {
        "name": "mytool",
        "version": "1.0",
        "description": "d",
        "container": "img:1",
        "command": "run {{ inputs.f }}",
        "inputs": {"f": {"format": "fastq"}},
        "outputs": {"o": {"format": "txt", "pattern": "*.txt"}},
    },
}


def _write_spec(tmp_path, doc=None) -> Path:
    p = tmp_path / "spec.bioledger.yaml"
    p.write_text(yaml.dump(doc or _SPEC))
    return p


class TestToolImport:
    def test_import_saves_to_store(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        result = runner.invoke(app, ["tool", "import", str(p)])
        assert result.exit_code == 0, result.output
        assert "Imported 'mytool'" in result.output
        from bioledger.toolspec.store import ToolStore

        assert ToolStore().has("mytool")

    def test_name_override(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        result = runner.invoke(
            app, ["tool", "import", str(p), "--name", "renamed"]
        )
        assert result.exit_code == 0
        assert "Imported 'renamed'" in result.output

    def test_dry_run_writes_nothing(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        result = runner.invoke(app, ["tool", "import", str(p), "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output
        assert "mytool" in result.output  # rendered YAML shown
        from bioledger.toolspec.store import ToolStore

        assert not ToolStore().has("mytool")

    def test_invalid_spec_rejected(self, iso_home, tmp_path):
        bad = {"execution": {"name": "", "command": ""}}
        p = _write_spec(tmp_path, bad)
        result = runner.invoke(app, ["tool", "import", str(p)])
        assert result.exit_code == 1
        assert "Not importing" in result.output

    def test_non_yaml_extension_rejected(self, iso_home, tmp_path):
        p = tmp_path / "tool.xml"
        p.write_text("<tool/>")
        result = runner.invoke(app, ["tool", "import", str(p)])
        assert result.exit_code == 1
        assert "Unsupported file type" in result.output


class TestToolValidate:
    def test_valid_spec(self, tmp_path):
        p = _write_spec(tmp_path)
        result = runner.invoke(app, ["tool", "validate", str(p)])
        assert result.exit_code == 0
        assert "is valid" in result.output

    def test_warnings_fail_under_strict(self, tmp_path):
        doc = dict(_SPEC)
        doc["execution"] = dict(_SPEC["execution"])
        del doc["execution"]["description"]  # warning: description recommended
        p = _write_spec(tmp_path, doc)
        assert runner.invoke(app, ["tool", "validate", str(p)]).exit_code == 0
        result = runner.invoke(app, ["tool", "validate", str(p), "--strict"])
        assert result.exit_code == 1
        assert "has issues" in result.output

    def test_invalid_spec_fails(self, tmp_path):
        p = _write_spec(
            tmp_path, {"execution": {"name": "t", "command": ""}}
        )
        result = runner.invoke(app, ["tool", "validate", str(p)])
        assert result.exit_code == 1


class TestToolListShow:
    def test_list_empty(self, iso_home):
        result = runner.invoke(app, ["tool", "list"])
        assert "No tools found" in result.output

    def test_list_shows_tools(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        runner.invoke(app, ["tool", "import", str(p)])
        result = runner.invoke(app, ["tool", "list"])
        assert "mytool" in result.output
        assert "img:1" in result.output

    def test_list_search_filter(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        runner.invoke(app, ["tool", "import", str(p)])
        assert "mytool" in runner.invoke(
            app, ["tool", "list", "--search", "myt"]
        ).output
        assert "No tools found" in runner.invoke(
            app, ["tool", "list", "--search", "zzzz"]
        ).output

    def test_show_details(self, iso_home, tmp_path):
        p = _write_spec(tmp_path)
        runner.invoke(app, ["tool", "import", str(p)])
        result = runner.invoke(app, ["tool", "show", "mytool"])
        assert result.exit_code == 0
        for fragment in ("mytool", "img:1", "run {{ inputs.f }}", "f:", "o:"):
            assert fragment in result.output

    def test_show_missing_tool(self, iso_home):
        result = runner.invoke(app, ["tool", "show", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestToolSearch:
    def test_unknown_source_rejected(self, iso_home):
        result = runner.invoke(
            app, ["tool", "search", "q", "--source", "snakemake"]
        )
        assert result.exit_code == 1
        assert "Unknown --source" in result.output

    def test_toolshed_results(self, iso_home, monkeypatch):
        fake = SimpleNamespace(
            tool_id="t1", name="FastQC", description="qc tool",
            repo_owner="iuc", repo_name="fastqc",
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.search_toolshed",
            lambda q: [fake],
        )
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.search_nfcore",
            lambda q: [],
        )
        result = runner.invoke(app, ["tool", "search", "fastqc"])
        assert result.exit_code == 0
        assert "FastQC" in result.output
        assert "iuc/fastqc" in result.output

    def test_nfcore_results(self, iso_home, monkeypatch):
        fake = SimpleNamespace(
            name="bwa/mem", subdirectory="bwa/mem", description="align"
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.search_toolshed",
            lambda q: [],
        )
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.search_nfcore",
            lambda q: [fake],
        )
        result = runner.invoke(
            app, ["tool", "search", "bwa", "--source", "nfcore"]
        )
        assert result.exit_code == 0
        assert "bwa/mem" in result.output

    def test_no_results(self, iso_home, monkeypatch):
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.search_toolshed",
            lambda q: [],
        )
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.search_nfcore",
            lambda q: [],
        )
        result = runner.invoke(app, ["tool", "search", "nothing"])
        assert result.exit_code == 0
        assert "No results" in result.output

    def test_backend_runtime_error_handled(self, iso_home, monkeypatch):
        def boom(q):
            raise RuntimeError("API down")

        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.search_toolshed", boom
        )
        result = runner.invoke(
            app, ["tool", "search", "x", "--source", "toolshed"]
        )
        assert result.exit_code == 0
        assert "API down" in result.output


# ---------------------------------------------------------------------------
# crystallize / package
# ---------------------------------------------------------------------------


def _completed_entry(**kw):
    kw.setdefault("run_status", "completed")
    return _entry(**kw)


class TestCrystallize:
    def test_prints_workflow(self, store, monkeypatch):
        s = _session(store)
        store.append_entry(s.id, _completed_entry())
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.crystallize.to_nextflow",
            lambda session, **kw: "WORKFLOW TEXT",
        )
        result = runner.invoke(app, ["crystallize", s.id])
        assert result.exit_code == 0
        assert "WORKFLOW TEXT" in result.output

    def test_entry_filter_uses_from_entries(self, store, monkeypatch):
        s = _session(store)
        e1, e2 = _completed_entry(), _completed_entry()
        store.append_entry(s.id, e1)
        store.append_entry(s.id, e2)
        captured = {}
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.crystallize.to_nextflow_from_entries",
            lambda entries, **kw: (
                captured.setdefault("ids", [e.id for e in entries]),
                "FILTERED",
            )[1],
        )
        result = runner.invoke(
            app, ["crystallize", s.id, "--entry", e1.id]
        )
        assert result.exit_code == 0
        assert captured["ids"] == [e1.id]
        assert "FILTERED" in result.output

    def test_companion_files_warn_in_text_mode(self, store, monkeypatch):
        s = _session(store)
        store.append_entry(
            s.id, _completed_entry(companion_files={"main.nf": "/x/main.nf"})
        )
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.crystallize.to_nextflow",
            lambda session, **kw: "WF",
        )
        result = runner.invoke(app, ["crystallize", s.id])
        assert result.exit_code == 0
        assert "companion files" in result.output

    def test_stage_calls_build_rocrate(self, store, tmp_path, monkeypatch):
        s = _session(store)
        calls = {}

        def fake_build(session, out, **kw):
            calls.update(session_id=session.id, out=out, kw=kw)
            return out

        monkeypatch.setattr(
            "bioledger.forges.crateforge.builder.build_rocrate", fake_build
        )
        stage = tmp_path / "staged"
        result = runner.invoke(
            app, ["crystallize", s.id, "--stage", str(stage)]
        )
        assert result.exit_code == 0
        assert calls["session_id"] == s.id
        assert calls["out"] == stage
        assert "Runnable crystallized workflow" in result.output

    def test_missing_session_fails(self, store):
        result = runner.invoke(app, ["crystallize", "nope1234"])
        assert result.exit_code == 1


class TestPackage:
    def test_package_writes_crate(self, store, tmp_path, monkeypatch):
        s = _session(store)
        calls = {}

        def fake_build(session, out, **kw):
            calls.update(out=out, kw=kw)
            return out

        monkeypatch.setattr(
            "bioledger.forges.crateforge.builder.build_rocrate", fake_build
        )
        out_dir = tmp_path / "crate"
        result = runner.invoke(
            app, ["package", s.id, "--output-dir", str(out_dir)]
        )
        assert result.exit_code == 0
        assert calls["out"] == out_dir
        assert "RO-Crate written" in result.output

    def test_package_default_output_dir(self, store, iso_home, monkeypatch):
        s = _session(store)
        calls = {}
        monkeypatch.setattr(
            "bioledger.forges.crateforge.builder.build_rocrate",
            lambda session, out, **kw: calls.setdefault("out", out) or out,
        )
        result = runner.invoke(app, ["package", s.id])
        assert result.exit_code == 0
        assert calls["out"] == iso_home / ".bioledger" / "crates" / s.id

    def test_package_entry_ids_forwarded(self, store, tmp_path, monkeypatch):
        s = _session(store)
        e = _completed_entry()
        store.append_entry(s.id, e)
        calls = {}
        monkeypatch.setattr(
            "bioledger.forges.crateforge.builder.build_rocrate",
            lambda session, out, **kw: calls.setdefault("kw", kw) or out,
        )
        result = runner.invoke(
            app,
            ["package", s.id, "-e", e.id, "--output-dir", str(tmp_path / "c")],
        )
        assert result.exit_code == 0
        assert calls["kw"]["entry_ids"] == [e.id]


# ---------------------------------------------------------------------------
# library / study commands
# ---------------------------------------------------------------------------


def _seed_tool_index(iso_home, entries):
    cache = iso_home / ".bioledger" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "toolspec_index.json").write_text(json.dumps(entries))


def _seed_study_index(iso_home, entries):
    cache = iso_home / ".bioledger" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "study_index.json").write_text(json.dumps(entries))


_TOOL_ENTRY = {
    "name": "samtools-sort",
    "family": "samtools",
    "version": "1.20",
    "description": "Sort BAM",
    "container": "img:1",
    "categories": ["alignment"],
    "inputs": ["bam"],
    "outputs": ["sorted"],
    "path": "samtools/samtools-sort",
}

_STUDY_ENTRY = {
    "accession": "PRJNA450813",
    "study_type": "experimental_data",
    "organism": "Leishmania donovani",
    "title": "Clinical isolates",
    "description": "d",
    "formats": ["fastq"],
    "file_count": 6,
}


class TestLibraryCommands:
    def test_list_empty(self, iso_home):
        _seed_tool_index(iso_home, [])
        result = runner.invoke(app, ["library", "list"])
        assert "No tools found" in result.output

    def test_list(self, iso_home):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        result = runner.invoke(app, ["library", "list"])
        assert result.exit_code == 0
        assert "samtools-sort" in result.output
        assert "samtools" in result.output

    def test_search(self, iso_home):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        result = runner.invoke(app, ["library", "search", "sort"])
        assert "samtools-sort" in result.output
        result = runner.invoke(app, ["library", "search", "zzz"])
        assert "No tools matching" in result.output

    def test_show(self, iso_home):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        result = runner.invoke(app, ["library", "show", "samtools-sort"])
        assert result.exit_code == 0
        for frag in ("samtools-sort", "1.20", "Sort BAM", "img:1", "bam"):
            assert frag in result.output

    def test_show_missing(self, iso_home):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        result = runner.invoke(app, ["library", "show", "ghost"])
        assert result.exit_code == 1

    def test_import(self, iso_home, monkeypatch):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        spec = SimpleNamespace(name="samtools-sort")
        monkeypatch.setattr(
            "bioledger.toolspec.store.ToolStore.import_from_library",
            lambda self, path, ref="main": spec,
        )
        result = runner.invoke(app, ["library", "import", "samtools-sort"])
        assert result.exit_code == 0
        assert "Imported 'samtools-sort'" in result.output

    def test_import_missing_tool(self, iso_home):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        result = runner.invoke(app, ["library", "import", "ghost"])
        assert result.exit_code == 1

    def test_import_fetch_failure(self, iso_home, monkeypatch):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])

        def boom(self, path, ref="main"):
            raise ValueError("network down")

        monkeypatch.setattr(
            "bioledger.toolspec.store.ToolStore.import_from_library", boom
        )
        result = runner.invoke(app, ["library", "import", "samtools-sort"])
        assert result.exit_code == 1
        assert "network down" in result.output

    def test_refresh(self, iso_home, monkeypatch):
        _seed_tool_index(iso_home, [_TOOL_ENTRY])
        monkeypatch.setattr(
            "bioledger.core.library.ToolLibrary.refresh", lambda self: None
        )
        result = runner.invoke(app, ["library", "refresh"])
        assert result.exit_code == 0
        assert "1 tools" in result.output


class TestStudyCommands:
    def test_list(self, iso_home):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        result = runner.invoke(app, ["study", "list"])
        assert result.exit_code == 0
        assert "PRJNA450813" in result.output
        # rich table truncates long cells at 80-col width
        assert "Leishmania" in result.output
        assert "experimental" in result.output

    def test_list_empty(self, iso_home):
        _seed_study_index(iso_home, [])
        result = runner.invoke(app, ["study", "list"])
        assert "No studies found" in result.output

    def test_search(self, iso_home):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        result = runner.invoke(app, ["study", "search", "leishmania"])
        assert "PRJNA450813" in result.output
        result = runner.invoke(app, ["study", "search", "zzz"])
        assert "No studies matching" in result.output

    def test_show(self, iso_home):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        result = runner.invoke(app, ["study", "show", "PRJNA450813"])
        assert result.exit_code == 0
        for frag in ("PRJNA450813", "experimental data", "Leishmania"):
            assert frag in result.output

    def test_show_missing(self, iso_home):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        result = runner.invoke(app, ["study", "show", "NONE"])
        assert result.exit_code == 1

    def test_load_with_download(self, iso_home, monkeypatch):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        mock_dl = AsyncMock()
        monkeypatch.setattr(
            "bioledger.core.library.StudyLibrary.download", mock_dl
        )
        result = runner.invoke(app, ["study", "load", "PRJNA450813"])
        assert result.exit_code == 0
        assert "downloaded" in result.output
        mock_dl.assert_awaited_once()
        assert mock_dl.await_args.kwargs["with_data"] is True

    def test_load_metadata_only(self, iso_home, monkeypatch):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        mock_dl = AsyncMock()
        monkeypatch.setattr(
            "bioledger.core.library.StudyLibrary.download", mock_dl
        )
        result = runner.invoke(
            app, ["study", "load", "PRJNA450813", "--no-download"]
        )
        assert result.exit_code == 0
        assert "metadata" in result.output
        assert mock_dl.await_args.kwargs["with_data"] is False

    def test_load_failure(self, iso_home, monkeypatch):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        mock_dl = AsyncMock(side_effect=ValueError("no manifest"))
        monkeypatch.setattr(
            "bioledger.core.library.StudyLibrary.download", mock_dl
        )
        result = runner.invoke(app, ["study", "load", "PRJNA450813"])
        assert result.exit_code == 1
        assert "no manifest" in result.output

    def test_refresh(self, iso_home, monkeypatch):
        _seed_study_index(iso_home, [_STUDY_ENTRY])
        monkeypatch.setattr(
            "bioledger.core.library.StudyLibrary.refresh", lambda self: None
        )
        result = runner.invoke(app, ["study", "refresh"])
        assert "1 studies" in result.output


# ---------------------------------------------------------------------------
# config validate
# ---------------------------------------------------------------------------


class TestConfigValidate:
    def test_ok_config(self, iso_home, monkeypatch):
        monkeypatch.setattr(
            "bioledger.core.engines.validate.validate_engine_config",
            lambda engines, specs=None: SimpleNamespace(ok=True),
        )
        monkeypatch.setattr(
            "bioledger.core.engines.validate.format_warnings",
            lambda r: "All engine configurations OK",
        )
        result = runner.invoke(app, ["config", "validate"])
        assert result.exit_code == 0

    def test_warnings_fail(self, iso_home, monkeypatch):
        warning = SimpleNamespace(
            engine="snakemake", message="no container", hint="h"
        )
        monkeypatch.setattr(
            "bioledger.core.engines.validate.validate_engine_config",
            lambda engines, specs=None: SimpleNamespace(
                ok=False, warnings=[warning]
            ),
        )
        monkeypatch.setattr(
            "bioledger.core.engines.validate.format_warnings",
            lambda r: "1 warning(s)",
        )
        result = runner.invoke(app, ["config", "validate"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_format_elapsed(self):
        from bioledger.apps.cli.main import _format_elapsed

        assert _format_elapsed(30) == "30s"
        assert _format_elapsed(90) == "1.5m"
        assert _format_elapsed(3700) == "1h 1m"

    def test_failure_snippet(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        result = SimpleNamespace(stderr="line1\nline2\n", stdout="")
        snippet = _failure_snippet(result, tmp_path)
        assert "line2" in snippet


class TestFailureSnippet:
    def _r(self, stderr="", stdout=""):
        return SimpleNamespace(stderr=stderr, stdout=stdout)

    def test_prefers_stderr_over_stdout(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        out = _failure_snippet(
            self._r(stderr="real error", stdout="noise"), tmp_path
        )
        assert "real error" in out and "noise" not in out

    def test_falls_back_to_stdout(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        out = _failure_snippet(
            self._r(stderr="  ", stdout="stdout err"), tmp_path
        )
        assert "stdout err" in out

    def test_no_output(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        assert _failure_snippet(self._r(), tmp_path) == "(no output captured)"

    def test_long_output_tail_biased(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        text = "head\n" + "x" * 5000 + "\ntail"
        out = _failure_snippet(self._r(stderr=text), tmp_path, max_chars=100)
        assert "head" not in out
        assert "tail" in out
        assert "showing last 100 chars of stderr" in out
        assert "stderr.log" in out

    def test_markup_escaped(self, tmp_path):
        from bioledger.apps.cli.main import _failure_snippet

        out = _failure_snippet(self._r(stderr="[bad]"), tmp_path)
        assert "[bad]" in out  # escaped, not interpreted


class TestPrintJobCompletion:
    def _entry(self, status, **kw):
        from bioledger.ledger.models import FileRef

        kw.setdefault("run_status", status)
        kw.setdefault("exit_code", 1 if status == "failed" else 0)
        return _entry(
            files=[
                FileRef(path="/r/out.bam", sha256="s", size_bytes=1, role="output"),
            ],
            **kw,
        )

    def test_completed_prints_outputs(self):
        from rich.console import Console

        from bioledger.apps.cli.main import _print_job_completion

        con = Console(record=True, width=120)
        _print_job_completion(con, self._entry("completed"))
        text = con.export_text()
        assert "Job finished" in text
        assert "out.bam" in text

    def test_failed_prints_stderr_tail(self, tmp_path):
        from bioledger.ledger.models import FileRef
        from rich.console import Console

        from bioledger.apps.cli.main import _print_job_completion

        log = tmp_path / "stderr.log"
        log.write_text("banner\n" + "x" * 10 + "\nreal failure")
        e = self._entry("failed")
        e.files.append(
            FileRef(path=str(log), sha256="s", size_bytes=1, role="log")
        )
        con = Console(record=True, width=120)
        _print_job_completion(con, e)
        text = con.export_text()
        assert "real failure" in text

    def test_failed_missing_log_no_snippet(self):
        from rich.console import Console

        from bioledger.apps.cli.main import _print_job_completion

        e = self._entry("failed")
        con = Console(record=True, width=120)
        _print_job_completion(con, e)
        assert "failed (exit 1)" in con.export_text()


class TestPrintJobStatus:
    def test_no_running_jobs(self):
        from rich.console import Console

        from bioledger.apps.cli.main import _print_job_status

        con = Console(record=True, width=120)
        _print_job_status(con, LedgerSession(name="s"))
        assert "No background jobs" in con.export_text()

    def test_lists_running_with_elapsed(self):
        from bioledger.ledger.models import LedgerSession
        from rich.console import Console

        from bioledger.apps.cli.main import _print_job_status

        s = LedgerSession(name="s")
        s.add(_entry(id="abcd1234", run_status="running"))
        con = Console(record=True, width=120)
        _print_job_status(con, s)
        text = con.export_text()
        assert "(abcd1234)" in text and "running for" in text


class _FakeAgent:
    def __init__(self, session, dataset=None, home_dir=None, tool_store=None):
        self.session = session
        self.dataset = dataset
        self.config = SimpleNamespace(home_dir=home_dir or Path("/nonexistent"))
        self.tool_store = tool_store or SimpleNamespace(
            load=lambda name: (_ for _ in ()).throw(KeyError(name))
        )


def _request(mapping: dict[str, str]):
    from bioledger.forges.analysisforge.agent import KeyValuePair, ToolRunRequest

    return ToolRunRequest(
        tool_name="t",
        rationale="r",
        input_mapping=[KeyValuePair(key=k, value=v) for k, v in mapping.items()],
    )


def _out_file(tmp_path, name):
    from bioledger.ledger.models import FileRef

    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("data")
    return FileRef(path=str(p), sha256="s", size_bytes=4, role="output")


class TestResolveInputs:
    def test_literal_path_wins(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        f = tmp_path / "real.bam"
        f.write_text("x")
        agent = _FakeAgent(LedgerSession(name="s"))
        files, parent = _resolve_inputs(agent, _request({"in": str(f)}))
        assert files["in"] == f
        assert parent is None

    def test_prior_output_exact_match_sets_parent(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        s = LedgerSession(name="s")
        e = _entry(id="entryAAA", files=[_out_file(tmp_path, "res.bam")])
        s.add(e)
        agent = _FakeAgent(s)
        files, parent = _resolve_inputs(agent, _request({"in": "res.bam"}))
        assert files["in"].name == "res.bam"
        assert parent == "entryAAA"

    def test_entry_prefix_disambiguates(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        s = LedgerSession(name="s")
        older = _entry(id="aaaa", files=[_out_file(tmp_path / "a", "dup.txt")])
        newer = _entry(id="bbbb", files=[_out_file(tmp_path / "b", "dup.txt")])
        s.add(older)
        s.add(newer)
        agent = _FakeAgent(s)
        files, parent = _resolve_inputs(
            agent, _request({"in": "aaaa/dup.txt"})
        )
        # hint targets the older entry specifically, not most-recent
        assert parent == "aaaa"
        assert files["in"].parent.name == "a"

    def test_dataset_exact_name(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs
        from bioledger.forges.isaforge.dataset import DataFile, DataSet

        isa = tmp_path / "isa"
        isa.mkdir()
        (isa / "reads.fastq").write_text("x")
        ds = DataSet(
            name="d",
            files=[DataFile(location="reads.fastq", format="fastq")],
            isa_tab_dir=isa,
        )
        agent = _FakeAgent(LedgerSession(name="s"), dataset=ds)
        files, _ = _resolve_inputs(agent, _request({"in": "reads.fastq"}))
        assert files["in"] == isa / "reads.fastq"

    def test_isatab_structural_file(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs
        from bioledger.forges.isaforge.dataset import DataSet

        isa = tmp_path / "isa"
        isa.mkdir()
        (isa / "s_study.txt").write_text("x")
        ds = DataSet(name="d", isa_tab_dir=isa)
        agent = _FakeAgent(LedgerSession(name="s"), dataset=ds)
        files, _ = _resolve_inputs(agent, _request({"in": "s_study.txt"}))
        assert files["in"] == isa / "s_study.txt"

    def test_datasets_dir_recursive_search(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        datasets = tmp_path / "datasets" / "GSE123"
        datasets.mkdir(parents=True)
        (datasets / "ref.fa").write_text("x")
        agent = _FakeAgent(LedgerSession(name="s"), home_dir=tmp_path)
        files, _ = _resolve_inputs(agent, _request({"in": "ref.fa"}))
        assert files["in"].name == "ref.fa"

    def test_fuzzy_substring_fallback(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        s = LedgerSession(name="s")
        s.add(_entry(files=[_out_file(tmp_path, "sorted_output.bam")]))
        agent = _FakeAgent(s)
        files, parent = _resolve_inputs(
            agent, _request({"in": "sorted_out"})
        )
        assert "sorted_output.bam" in str(files["in"])

    def test_format_fallback_with_compression_stripped(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs
        from bioledger.toolspec.models import (
            ExecutionSpec, ParamType, ToolInput, ToolSpec,
        )

        s = LedgerSession(name="s")
        s.add(_entry(files=[_out_file(tmp_path, "x.bam.gz")]))
        spec = ToolSpec(
            execution=ExecutionSpec(
                name="t",
                command="c",
                inputs={"in": ToolInput(type=ParamType.FILE, format="bam")},
            )
        )
        store = SimpleNamespace(load=lambda name: spec)
        agent = _FakeAgent(s, tool_store=store)
        files, parent = _resolve_inputs(agent, _request({"in": "nomatch"}))
        # .gz stripped → extension bam matches declared format
        assert files["in"].name == "x.bam.gz"
        assert parent == s.entries[0].id

    def test_dataset_substring_fallback(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs
        from bioledger.forges.isaforge.dataset import DataFile, DataSet

        isa = tmp_path / "isa"
        isa.mkdir()
        (isa / "patient_reads_R1.fastq").write_text("x")
        ds = DataSet(
            name="d",
            files=[DataFile(
                location="patient_reads_R1.fastq", format="fastq"
            )],
            isa_tab_dir=isa,
        )
        agent = _FakeAgent(LedgerSession(name="s"), dataset=ds)
        files, _ = _resolve_inputs(agent, _request({"in": "patient"}))
        assert files["in"].name == "patient_reads_R1.fastq"

    def test_unresolvable_raises(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs

        agent = _FakeAgent(LedgerSession(name="s"))
        with pytest.raises(ValueError, match="Cannot resolve input"):
            _resolve_inputs(agent, _request({"in": "ghost.bam"}))

    def test_dataset_format_match(self, tmp_path):
        from bioledger.apps.cli.main import _resolve_inputs
        from bioledger.forges.isaforge.dataset import DataFile, DataSet

        isa = tmp_path / "isa"
        isa.mkdir()
        (isa / "weirdname.dat").write_text("x")
        ds = DataSet(
            name="d",
            files=[DataFile(location="weirdname.dat", format="bam")],
            isa_tab_dir=isa,
        )
        agent = _FakeAgent(LedgerSession(name="s"), dataset=ds)
        files, _ = _resolve_inputs(agent, _request({"in": "bam"}))
        assert files["in"].name == "weirdname.dat"


class TestAnalysisChat:
    """Thin coverage of the interactive loop's non-LLM commands."""

    async def _run_chat(self, iso_home, monkeypatch, inputs):
        import bioledger.apps.cli.main as main
        from bioledger.forges.isaforge.dataset import DataSet

        store = LedgerStore()
        session = _session(store, name="chat-s")

        fake_console = SimpleNamespace(
            input=MagicMock(side_effect=inputs),
            print=MagicMock(),
        )
        agent = MagicMock()
        agent.session = session
        agent.dataset = None
        agent.poll_pending_jobs.return_value = []
        agent.review_entries.return_value = [
            {
                "kind": "tool_run",
                "id": "e1",
                "tool": "fastqc",
                "outputs": ["/x/out.html"],
            },
            {"kind": "data_import", "id": "e2", "tool": "study"},
            {"kind": "user_note", "notes": "a note"},
        ]

        monkeypatch.setattr(main, "console", fake_console)
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.agent.AnalysisForgeAgent",
            lambda *a, **k: agent,
        )
        await main._analysis_chat(session.id)
        return fake_console, store.load_session(
            session.id, include_messages=True
        )

    async def test_quit_exits(self, iso_home, monkeypatch):
        from unittest.mock import MagicMock

        con, session = await self._run_chat(
            iso_home, monkeypatch, ["quit"]
        )
        con.print.assert_called()

    async def test_eof_exits(self, iso_home, monkeypatch):
        con, session = await self._run_chat(
            iso_home, monkeypatch, [EOFError()]
        )
        con.print.assert_called()

    async def test_status_and_review_commands(self, iso_home, monkeypatch):
        con, session = await self._run_chat(
            iso_home,
            monkeypatch,
            ["status", "review", "quit"],
        )
        printed = " ".join(
            str(c.args[0]) for c in con.print.call_args_list if c.args
        )
        assert "TOOL" in printed and "DATA" in printed
        assert "No background jobs" in printed
        # 'status' short-circuits before message recording; 'review' is
        # recorded as a user message
        msgs = [m.content for m in session.chat_messages]
        assert msgs == ["review"]
