"""Tests for AnalysisForgeAgent: registered agent tools, dataset loading,
suggestions, entry review, and engine status — all with mocked LLM agents."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import bioledger.forges.analysisforge.agent as agent_mod
from bioledger.config import BioLedgerConfig
from bioledger.forges.analysisforge.agent import (
    AnalysisForgeAgent,
    ToolRunRequest,
)
from bioledger.ledger.models import (
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)


class _FakeAgent:
    """Stand-in for a pydantic-ai Agent: records tools + canned output."""

    def __init__(self):
        self.tools = {}
        self.prompts = []
        self.output = None

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn

    async def run(self, prompt, deps=None, message_history=None):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


@pytest.fixture
def made_agents():
    return {}


@pytest.fixture
def agent(tmp_path, monkeypatch, made_agents):
    """An AnalysisForgeAgent with mocked LLM agents, tool store, library."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = BioLedgerConfig(home_dir=tmp_path / "bl")
    session = LedgerSession(name="s")
    store = MagicMock()

    def fake_make_agent(cfg, **kw):
        a = _FakeAgent()
        made_agents[kw.get("task")] = a
        return a

    monkeypatch.setattr(agent_mod, "make_agent", fake_make_agent)
    monkeypatch.setattr(
        agent_mod, "ToolStore",
        lambda: SimpleNamespace(list_all=lambda: []),
    )
    monkeypatch.setattr(
        "bioledger.core.library.ToolLibrary",
        lambda cache_dir=None: SimpleNamespace(
            list_cached=lambda: [], import_tool=lambda n: None
        ),
    )
    a = AnalysisForgeAgent(config, session, store)
    a._test_store = store
    return a


def _ctx(agent):
    return SimpleNamespace(
        deps=SimpleNamespace(session=agent.session, config=agent.config)
    )


def _entry(kind=EntryKind.TOOL_RUN, **kw):
    kw.setdefault("kind", kind)
    kw.setdefault("exit_code", 0)
    return LedgerEntry(**kw)


def _out(tmp_path, name, content="data"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return FileRef(path=str(p), sha256="s", size_bytes=len(content), role="output")


class TestInit:
    def test_registers_chat_tools(self, agent):
        assert set(agent._chat_agent.tools) == {
            "fetch_entry_outputs",
            "check_job_status",
            "list_available_engines",
            "search_toolshed",
            "search_nfcore",
            "search_snakemake_wrappers",
        }

    def test_tool_select_agent_created(self, agent, made_agents):
        assert "analysis_suggest" in made_agents
        assert agent._tool_select_agent is made_agents["analysis_suggest"]


class TestFetchEntryOutputs:
    def test_no_matching_entry(self, agent):
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        assert "No entry found" in tool(_ctx(agent), "nope")

    def test_non_run_entry_kind(self, agent):
        agent.session.add(_entry(kind=EntryKind.USER_NOTE, id="n1"))
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        assert "not a tool/script run" in tool(_ctx(agent), "n1")

    def test_no_output_files(self, agent):
        agent.session.add(_entry(id="e1", tool_spec_name="t"))
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        assert "has no output files" in tool(_ctx(agent), "e1")

    def test_reads_small_file(self, agent, tmp_path):
        agent.session.add(
            _entry(id="e1", files=[_out(tmp_path, "o.txt", "hello")])
        )
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        out = tool(_ctx(agent), "e1")
        assert "--- o.txt ---" in out and "hello" in out

    def test_large_file_not_displayed(self, agent, tmp_path):
        p = tmp_path / "big.bin"
        p.write_text("x" * 60_000)
        agent.session.add(
            _entry(
                id="e1",
                files=[FileRef(path=str(p), sha256="s", size_bytes=60_000, role="output")],
            )
        )
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        assert "too large to display" in tool(_ctx(agent), "e1")

    def test_missing_file_reported(self, agent):
        agent.session.add(
            _entry(
                id="e1",
                files=[FileRef(path="/ghost/f.txt", sha256="s", size_bytes=1, role="output")],
            )
        )
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        assert "file not found" in tool(_ctx(agent), "e1")

    def test_more_than_max_outputs_listed(self, agent, tmp_path):
        files = [_out(tmp_path, f"f{i}.txt") for i in range(13)]
        agent.session.add(_entry(id="e1", files=files))
        tool = agent._chat_agent.tools["fetch_entry_outputs"]
        out = tool(_ctx(agent), "e1")
        assert "Showing 10 of 13 outputs" in out
        assert "f10.txt" in out  # excluded names preview


class TestCheckJobStatus:
    def test_empty_session(self, agent):
        tool = agent._chat_agent.tools["check_job_status"]
        assert "No tool/script runs" in tool(_ctx(agent))

    def test_lists_runs(self, agent, tmp_path):
        agent.session.add(
            _entry(id="e1", tool_spec_name="fastqc",
                   files=[_out(tmp_path, "o.txt")], run_status="running")
        )
        agent.session.add(_entry(kind=EntryKind.USER_NOTE))
        tool = agent._chat_agent.tools["check_job_status"]
        out = tool(_ctx(agent))
        assert "e1 (fastqc)" in out and "status=running" in out

    def test_output_list_capped(self, agent, tmp_path):
        files = [_out(tmp_path, f"f{i}.txt") for i in range(8)]
        agent.session.add(_entry(id="e1", files=files))
        tool = agent._chat_agent.tools["check_job_status"]
        assert "(8 total)" in tool(_ctx(agent))


class TestEngineStatusTool:
    def test_returns_status(self, agent):
        tool = agent._chat_agent.tools["list_available_engines"]
        out = tool(_ctx(agent))
        assert "nextflow" in out and "snakemake" in out

    def test_installed_states(self, agent, monkeypatch):
        monkeypatch.setattr(
            agent_mod.shutil, "which",
            lambda b: "/bin/nextflow" if b == "nextflow" else None,
        )
        monkeypatch.setattr(
            agent_mod.importlib.util, "find_spec",
            lambda m: object() if m == "snakemake" else None,
        )
        status = agent._build_engine_status()
        assert "nextflow installed (binary found)" in status
        assert "snakemake installed (package installed, binary not on PATH)" in status
        assert "galaxy (not installed)" in status


class TestSearchTools:
    def test_toolshed_results(self, agent, monkeypatch):
        r = SimpleNamespace(
            tool_id="tid", name="n", description="d",
            repo_owner="o", repo_name="r",
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.search_toolshed",
            lambda q: [r],
        )
        out = agent._chat_agent.tools["search_toolshed"](_ctx(agent), "q")
        assert "tid: n — d (repo: o/r)" in out

    def test_toolshed_empty_and_error(self, agent, monkeypatch):
        import bioledger.core.engines.toolshed_search as ts

        monkeypatch.setattr(ts, "search_toolshed", lambda q: [])
        tool = agent._chat_agent.tools["search_toolshed"]
        assert "No Tool Shed results" in tool(_ctx(agent), "q")

        def boom(q):
            raise RuntimeError("down")

        monkeypatch.setattr(ts, "search_toolshed", boom)
        assert "unavailable" in tool(_ctx(agent), "q")

    def test_nfcore_results(self, agent, monkeypatch):
        r = SimpleNamespace(name="n", subdirectory="s", description="d")
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.search_nfcore",
            lambda q: [r],
        )
        out = agent._chat_agent.tools["search_nfcore"](_ctx(agent), "q")
        assert "n (s): d" in out

    def test_snakemake_results(self, agent, monkeypatch):
        r = SimpleNamespace(name="n", path="p", description="d")
        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_search.search_wrappers",
            lambda q: [r],
        )
        out = agent._chat_agent.tools["search_snakemake_wrappers"](
            _ctx(agent), "q"
        )
        assert "n (p): d" in out

    def test_snakemake_error(self, agent, monkeypatch):
        def boom(q):
            raise RuntimeError("down")

        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_search.search_wrappers", boom
        )
        out = agent._chat_agent.tools["search_snakemake_wrappers"](
            _ctx(agent), "q"
        )
        assert "unavailable" in out


class TestLoadDataset:
    async def test_isatab_dir_path(self, agent, tmp_path, monkeypatch):
        data = tmp_path / "data.txt"
        data.write_text("x")
        from bioledger.forges.isaforge.dataset import DataFile, DataSet

        ds = DataSet(
            name="ds",
            files=[DataFile(location="data.txt", format="txt")],
            organisms=["H"],
            assay_type="rna-seq",
            isa_tab_dir=tmp_path,
        )
        monkeypatch.setattr(
            agent_mod, "load_dataset_from_isatab", lambda p, **kw: ds
        )
        out = await agent.load_dataset(tmp_path)
        assert agent.dataset is out
        entry = agent.session.entries[0]
        assert entry.kind == EntryKind.DATA_IMPORT
        assert entry.params["organisms"] == ["H"]
        assert entry.files[0].role == "input"
        assert entry.files[0].sha256  # hashed
        agent._test_store.save_session.assert_called_once()

    async def test_csv_path_converts_first(self, agent, tmp_path, monkeypatch):
        csv = tmp_path / "samples.csv"
        csv.write_text("sample\ns1\n")
        converted = []

        async def fake_convert(csv_path, out_dir, config=None):
            converted.append((csv_path, out_dir))

        monkeypatch.setattr(agent_mod, "csv_to_isatab", fake_convert)
        from bioledger.forges.isaforge.dataset import DataSet

        monkeypatch.setattr(
            agent_mod,
            "load_dataset_from_isatab",
            lambda p, **kw: DataSet(name="d", isa_tab_dir=p),
        )
        await agent.load_dataset(csv)
        assert converted[0][1] == agent.config.home_dir / "datasets" / "samples"
        entry = agent.session.entries[0]
        assert "converted to ISA-Tab" in entry.params["source"]

    async def test_datasets_dir_fallback(self, agent, tmp_path, monkeypatch):
        named = agent.config.home_dir / "datasets" / "mystudy"
        named.mkdir(parents=True)
        from bioledger.forges.isaforge.dataset import DataSet

        seen = []

        def fake_load(p, **kw):
            seen.append(p)
            return DataSet(name="d")

        monkeypatch.setattr(
            agent_mod, "load_dataset_from_isatab", fake_load
        )
        await agent.load_dataset(Path("/nonexistent/mystudy"))
        assert seen == [named]


class TestDownloadRemote:
    async def test_no_dataset_raises(self, agent):
        with pytest.raises(ValueError, match="No dataset"):
            await agent.download_remote(Path("/tmp/x"))

    async def test_delegates(self, agent, tmp_path, monkeypatch):
        from bioledger.forges.isaforge.dataset import DataSet

        ds = DataSet(name="d")
        agent.dataset = ds
        new_ds = DataSet(name="d2")

        async def fake_dl(dataset, dl_dir, user_confirmed):
            assert user_confirmed
            return new_ds

        monkeypatch.setattr(agent_mod, "download_remote_files", fake_dl)
        out = await agent.download_remote(tmp_path)
        assert out is new_ds
        assert agent.dataset is new_ds


class TestSuggestWorkflow:
    async def test_no_dataset_raises(self, agent):
        with pytest.raises(ValueError, match="No dataset"):
            await agent.suggest_workflow()

    async def test_delegates(self, agent, monkeypatch):
        from bioledger.forges.isaforge.dataset import DataSet

        agent.dataset = DataSet(name="d")

        async def fake_suggest(dataset, goal, deps):
            return {"ok": goal}

        monkeypatch.setattr(
            agent_mod, "suggest_analysis_for_dataset", fake_suggest
        )
        out = await agent.suggest_workflow("goal-1")
        assert out == {"ok": "goal-1"}


class TestSuggestNextTool:
    async def test_builds_context_and_returns_request(
        self, agent, tmp_path, monkeypatch
    ):
        from bioledger.forges.isaforge.dataset import DataFile, DataSet
        from bioledger.toolspec.models import (
            ExecutionSpec,
            ParamType,
            ToolInput,
            ToolSpec,
        )

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="align",
                command="align {{inputs.reads}}",
                description="Aligns",
                inputs={
                    "reads": ToolInput(
                        type=ParamType.FILE, format="fastq", required=True
                    )
                },
            )
        )
        agent.tool_store = SimpleNamespace(list_all=lambda: [spec])
        agent._tool_library = SimpleNamespace(
            list_cached=lambda: [
                {"name": "lib_tool", "description": "from lib"}
            ]
        )
        agent.dataset = DataSet(
            name="ds",
            file_formats=["fastq"],
            files=[DataFile(location="r.fastq", format="fastq")],
        )
        agent.session.add(
            _entry(id="prev1", tool_spec_name="t",
                   files=[_out(tmp_path, "o.bam")])
        )
        req = ToolRunRequest(tool_name="align", rationale="r")
        agent._tool_select_agent.output = req
        out = await agent.suggest_next_tool("run alignment")
        assert out is req
        prompt = agent._tool_select_agent.prompts[0]
        assert "run alignment" in prompt
        assert "align" in prompt and "lib_tool" in prompt
        assert "prev1" in prompt and "o.bam" in prompt
        assert "r.fastq" in prompt


class TestReviewEntries:
    def test_summary_keys_and_data_import_label(self, agent, tmp_path):
        agent.session.add(
            _entry(
                id="e1", tool_spec_name="fastqc",
                files=[_out(tmp_path, "o.txt")],
            )
        )
        agent.session.add(
            _entry(kind=EntryKind.DATA_IMPORT, id="e2",
                   params={"summary": "Loaded X"})
        )
        out = agent.review_entries()
        assert out[0]["kind"] == "tool_run"
        assert out[0]["outputs"] and out[0]["id"] == "e1"
        assert out[1]["notes"] == "Loaded X"


class TestSessionSummary:
    def test_empty(self, agent):
        assert agent._session_summary() == "  (no entries yet)"

    def test_tool_run_and_import_lines(self, agent, tmp_path):
        agent.session.add(
            _entry(id="e1", tool_spec_name="t",
                   files=[_out(tmp_path, "o.txt")], exit_code=0)
        )
        agent.session.add(
            _entry(kind=EntryKind.DATA_IMPORT, id="e2", notes="loaded")
        )
        out = agent._session_summary()
        assert "[e1] t" in out and "exit=0" in out
        assert "[e2] data_import: loaded" in out


class TestSubmitDiscovered:
    """Cover the toolshed/nfcore/snakemake branches of submit_discovered."""

    def _wire(self, agent, tmp_path, monkeypatch, input_mapping=None):
        """Common mocks: session dir, input prep, staging, adapter."""
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor.get_session_dir",
            lambda home, sid: session_dir,
        )
        monkeypatch.setattr(
            agent_mod,
            "_prepare_engine_inputs",
            lambda m: ({}, dict(m)),
        )
        staged = []

        def fake_stage_companion(companions, run_dir, entry_id):
            staged.append((dict(companions), run_dir, entry_id))

        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor.stage_companion_files",
            fake_stage_companion,
        )
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor._stage_inputs",
            lambda files, sdir, rdir: {},
        )
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor._build_input_file_refs",
            lambda names, rdir: [],
        )
        written = []
        monkeypatch.setattr(
            agent_mod,
            "_write_engine_metadata_sidecar",
            lambda sdir, eid, handle: written.append((eid, handle)),
        )
        adapter = MagicMock()
        adapter.submit_spec.return_value = SimpleNamespace(
            run_id="h1", metadata={"engine_version": "1.0"}
        )
        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda engine, cfg: adapter,
        )
        return session_dir, staged, written, adapter

    def test_toolshed_branch(self, agent, tmp_path, monkeypatch):
        from bioledger.toolspec.models import (
            ExecutionSpec,
            ParamType,
            ToolInput,
        )

        session_dir, staged, written, adapter = self._wire(
            agent, tmp_path, monkeypatch, {"reads": "/x/r.fq"}
        )
        tool_dir = tmp_path / "toolxml"
        tool_dir.mkdir()
        (tool_dir / "main.xml").write_text("<tool/>")
        (tool_dir / "macros.xml").write_text("<macros/>")
        spec = ExecutionSpec(
            name="bowtie2",
            command="unused",
            inputs={"reads": ToolInput(type=ParamType.FILE)},
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.download_tool",
            lambda o, r, revision=None, dest_dir=None: (tool_dir, "rev42"),
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.parse_tool_to_spec",
            lambda d: spec,
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.find_main_tool_xml",
            lambda d: tool_dir / "main.xml",
        )

        entry, handle = agent.submit_discovered(
            "bt2", "toolshed:devteam/bowtie2@rev42",
            {"reads": "/x/r.fq"}, {"mismatches": "2"},
        )
        assert entry.executor == "galaxy"
        assert entry.discovered_source == "toolshed:devteam/bowtie2@rev42"
        assert entry.discovered_revision == "rev42"
        assert entry.run_handle_id == "h1"
        assert entry.tool_spec_name == "bt2"  # tool_name override applied
        cmd = entry.tool_spec_snapshot["command"]
        assert "planemo run" in cmd and entry.id in cmd
        assert "__ENTRY_ID__" not in cmd
        # job.yml only includes provided input, not arbitrary params
        assert "path: {{ inputs.reads }}" in cmd
        assert "mismatches: {{ parameters.mismatches }}" in cmd
        # both XML files staged as companions
        assert staged[0][0].keys() == {"main.xml", "macros.xml"}
        assert adapter.submit_spec.called

    def test_toolshed_invalid_source(self, agent, tmp_path, monkeypatch):
        self._wire(agent, tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="Invalid toolshed source"):
            agent.submit_discovered(
                "t", "toolshed:noowner", {}, {}
            )

    def test_nfcore_branch(self, agent, tmp_path, monkeypatch):
        session_dir, staged, written, adapter = self._wire(
            agent, tmp_path, monkeypatch, {"reads": "/x/r.fq"}
        )

        def fake_install(subdir, dest_dir, revision=None):
            module_dir = dest_dir
            (module_dir / "sub").mkdir(parents=True)
            (module_dir / "main.nf").write_text("process T {}")
            (module_dir / "sub" / "f.txt").write_text("x")
            return module_dir, "sha999"

        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.install_nfcore_module",
            fake_install,
        )
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.parse_meta_yml",
            lambda d: {
                "inputs": ["reads"],
                "outputs": {"bam": "*.bam"},
                "container": "img:1",
                "process_name": "T",
                "input_channels": [
                    {"name": "reads", "kind": "file", "has_meta": False}
                ],
                "parameters": {},
            },
        )
        monkeypatch.setattr(
            "bioledger.core.engines.nfcore_search.generate_nfcore_wrapper",
            lambda sub, mdir, meta, eid: ("wrapper // __ENTRY_ID__", "cfg"),
        )

        entry, handle = agent.submit_discovered(
            "", "nfcore:bowtie2/align", {"reads": "/x/r.fq"}, {}
        )
        assert entry.executor == "nextflow"
        assert entry.discovered_revision == "sha999"
        assert entry.tool_spec_name == "bowtie2_align"  # derived name
        cmd = entry.tool_spec_snapshot["command"]
        assert "nextflow run" in cmd and entry.id in cmd
        assert "--reads {{ inputs.reads | shquote }}" in cmd
        # wrapper + module tree + generated config staged
        keys = staged[0][0]
        assert "wrapper_main.nf" in keys and "nextflow.config" in keys
        assert any("sub" in k or "main.nf" in k for k in keys)

    def test_snakemake_branch(self, agent, tmp_path, monkeypatch):
        from bioledger.toolspec.models import (
            ExecutionSpec,
            ParamType,
            ToolInput,
            ToolParameter,
        )

        session_dir, staged, written, adapter = self._wire(
            agent, tmp_path, monkeypatch, {"reads": "/x/r.fq"}
        )

        def fake_dl(wpath, revision=None, dest_dir=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            return dest_dir, "v9"

        spec = ExecutionSpec(
            name="w",
            command="unused",
            inputs={"reads": ToolInput(type=ParamType.FILE)},
            parameters={
                "extra": ToolParameter(type=ParamType.STRING, default="d"),
                "unused": ToolParameter(type=ParamType.STRING, default="d"),
            },
        )
        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_search.download_wrapper",
            fake_dl,
        )
        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_search.parse_wrapper_to_spec",
            lambda d, w: spec,
        )
        monkeypatch.setattr(
            "bioledger.core.engines.snakemake_search.generate_wrapper_snakefile",
            lambda w, s, revision=None: "# snakefile",
        )

        entry, _ = agent.submit_discovered(
            "myw", "snakemake:bio/fastqc@v9",
            {"reads": "/x/r.fq"}, {"extra": "e"},
        )
        assert entry.executor == "snakemake"
        assert entry.discovered_revision == "v9"
        cmd = entry.tool_spec_snapshot["command"]
        assert "snakemake -s" in cmd and entry.id in cmd
        assert "__ENTRY_ID__" not in cmd
        # provided input + provided param land in --config; the unprovided
        # optional param does not
        assert 'reads="{{ inputs.reads }}"' in cmd
        assert 'extra="{{ parameters.extra }}"' in cmd
        assert "unused" not in cmd
        assert "--use-conda --use-apptainer" in cmd or "--use-apptainer" in cmd
        assert staged[0][0].keys() == {"Snakefile"}

    def test_unknown_source_raises(self, agent, tmp_path, monkeypatch):
        self._wire(agent, tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="Unknown discovered_source"):
            agent.submit_discovered("t", "weird:x", {}, {})

    def test_submit_failure_marks_entry_failed(
        self, agent, tmp_path, monkeypatch
    ):
        from bioledger.toolspec.models import ExecutionSpec

        session_dir, staged, written, adapter = self._wire(
            agent, tmp_path, monkeypatch
        )
        adapter.submit_spec.side_effect = RuntimeError("engine boom")
        tool_dir = tmp_path / "t"
        tool_dir.mkdir()
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.download_tool",
            lambda o, r, revision=None, dest_dir=None: (tool_dir, "r"),
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.parse_tool_to_spec",
            lambda d: ExecutionSpec(name="x", command="c"),
        )
        monkeypatch.setattr(
            "bioledger.core.engines.toolshed_search.find_main_tool_xml",
            lambda d: tool_dir / "missing.xml",
        )
        with pytest.raises(RuntimeError, match="engine boom"):
            agent.submit_discovered("t", "toolshed:o/r", {}, {})
        entry = agent.session.entries[-1]
        assert entry.run_status == "failed"
        assert entry.exit_code == 1
        assert "Engine submit failed" in entry.notes
        agent._test_store.save_session.assert_called()


class TestPrepareEngineInputs:
    def test_plain_path_normalizes_to_basename(self, tmp_path):
        src = tmp_path / "in.bam"
        src.write_text("x")
        files, norm = agent_mod._prepare_engine_inputs({"reads": str(src)})
        assert files == {"reads": src}
        assert norm == {"reads": "in.bam"}

    def test_glob_expands_members(self, tmp_path):
        for n in ("a.bam", "b.bam"):
            (tmp_path / n).write_text("x")
        files, norm = agent_mod._prepare_engine_inputs(
            {"reads": str(tmp_path / "*.bam")}
        )
        # normalized keeps the glob basename for crate channel wiring
        assert norm == {"reads": "*.bam"}
        assert set(files) == {"reads__0", "reads__1"}

    def test_unmatched_glob_left_raw(self, tmp_path):
        files, norm = agent_mod._prepare_engine_inputs(
            {"reads": str(tmp_path / "*.bam")}
        )
        assert files == {}
        assert norm == {"reads": str(tmp_path / "*.bam")}


class TestEngineMetadataSidecar:
    def test_roundtrip(self, tmp_path):
        handle = SimpleNamespace(
            work_dir=tmp_path / "w",
            run_id="h1",
            executor="nextflow",
            submitted_at="2024-01-01",  # no isoformat attr → str()
            status="running",
            process_start_time=123.0,
            metadata={"planemo_pid": 42},
        )
        agent_mod._write_engine_metadata_sidecar(tmp_path, "e1", handle)
        meta = agent_mod._read_engine_metadata_sidecar(tmp_path, "e1")
        assert meta["run_id"] == "h1"
        assert meta["process_start_time"] == 123.0
        assert meta["planemo_pid"] == 42
        assert meta["submitted_at"] == "2024-01-01"

    def test_missing_returns_none(self, tmp_path):
        assert agent_mod._read_engine_metadata_sidecar(tmp_path, "x") is None

    def test_corrupt_returns_none(self, tmp_path):
        (tmp_path / "_engine_metadata_e.json").write_text("{bad")
        assert (
            agent_mod._read_engine_metadata_sidecar(tmp_path, "e") is None
        )


class TestFinalizeEngineEntry:
    def _status(self, state, message=None):
        return SimpleNamespace(state=state, message=message)

    def _setup(self, tmp_path, monkeypatch, discover=None, exit_code=None):
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor._discover_outputs",
            lambda spec, wdir, m, s, exclude_paths=None, exclude_dirs=None:
                discover or [],
        )
        monkeypatch.setattr(
            "bioledger.forges.analysisforge.executor._hash_file",
            lambda p: "sha",
        )
        monkeypatch.setattr(
            "bioledger.core.engines._subprocess.read_exit_code",
            lambda w: exit_code,
        )
        return work

    def _entry_with_spec(self, tmp_path):
        from bioledger.toolspec.models import ExecutionSpec

        return _entry(
            id="e1",
            tool_spec_snapshot=ExecutionSpec(name="t", command="c").model_dump(),
        )

    def test_collect_exception_tolerated(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        entry = self._entry_with_spec(tmp_path)
        adapter = MagicMock()
        adapter.collect.side_effect = RuntimeError("collect boom")
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work, adapter, self._status("completed")
        )
        assert out.run_status == "completed"
        assert out.exit_code == 0

    def test_bad_spec_snapshot_early_return(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        entry = _entry(id="e1", tool_spec_snapshot={"bogus": 1})
        adapter = MagicMock()
        adapter.collect.return_value = []
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work, adapter, self._status("failed", "oops")
        )
        assert out.run_status == "failed"
        assert out.exit_code == 1
        assert "oops" in out.notes

    def test_unknown_state_marks_failed_with_warning(
        self, tmp_path, monkeypatch
    ):
        work = self._setup(tmp_path, monkeypatch)
        entry = _entry(id="e1", tool_spec_snapshot={"bogus": 1})
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work, MagicMock(collect=lambda h: []),
            self._status("unknown"),
        )
        assert out.run_status == "failed"
        assert out.exit_code is None

    def test_discovers_outputs_and_collect_extras(
        self, tmp_path, monkeypatch
    ):
        out_file = tmp_path / "work" / "res.txt"
        collected_file = tmp_path / "collected.txt"
        collected_file.write_text("c")
        work = self._setup(
            tmp_path, monkeypatch, discover=[out_file], exit_code=0
        )
        out_file.write_text("o")
        entry = self._entry_with_spec(tmp_path)
        adapter = MagicMock()
        adapter.collect.return_value = [{"path": str(collected_file)}]
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work, adapter, self._status("completed")
        )
        paths = {f.path for f in out.files if f.role == "output"}
        assert str(out_file) in paths
        assert str(collected_file) in paths  # collect() extra included
        assert all(f.sha256 == "sha" for f in out.files)

    def test_sidecar_exit_code_preferred(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch, exit_code=7)
        entry = self._entry_with_spec(tmp_path)
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work,
            MagicMock(collect=lambda h: []), self._status("completed"),
        )
        assert out.exit_code == 7

    def test_unknown_appends_warning_note(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        entry = self._entry_with_spec(tmp_path)
        out = agent_mod._finalize_engine_entry(
            entry, MagicMock(), work,
            MagicMock(collect=lambda h: []), self._status("unknown"),
        )
        assert "uncertain" in out.notes


class TestGetToolSpec:
    def test_local_hit_no_import(self, agent):
        spec = object()
        agent.tool_store = SimpleNamespace(
            has=lambda n: True, load=lambda n: spec
        )
        agent._tool_library = SimpleNamespace(
            get=lambda n: pytest.fail("should not query library")
        )
        assert agent.get_tool_spec("t") is spec

    def test_library_auto_import(self, agent):
        imported = []
        spec = object()
        agent.tool_store = SimpleNamespace(
            has=lambda n: False,
            load=lambda n: spec,
            import_from_library=lambda path: imported.append(path),
        )
        agent._tool_library = SimpleNamespace(
            get=lambda n: {"path": "lib/t"}
        )
        assert agent.get_tool_spec("t") is spec
        assert imported == ["lib/t"]


class TestRunToolWithLogging:
    async def test_async_mode_submits(self, agent, tmp_path, monkeypatch):
        from bioledger.toolspec.models import (
            ExecutionMode,
            ExecutionSpec,
            ToolSpec,
        )

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="t", command="c",
                suggested_mode=ExecutionMode.ASYNC,
            )
        )
        agent.tool_store = SimpleNamespace(
            has=lambda n: True, load=lambda n: spec
        )
        entry = _entry(id="e1")
        monkeypatch.setattr(
            agent_mod, "submit_tool",
            lambda s, sp, i, sd, params=None, parent_id=None: entry,
        )
        out_entry, result = await agent.run_tool_with_logging(
            "t", {}, tmp_path
        )
        assert out_entry is entry and result is None
        agent._test_store.save_session.assert_called()

    async def test_blocking_mode_runs(self, agent, tmp_path, monkeypatch):
        from bioledger.toolspec.models import (
            ExecutionMode,
            ExecutionSpec,
            ToolSpec,
        )

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="t", command="c",
                suggested_mode=ExecutionMode.BLOCKING,
            )
        )
        agent.tool_store = SimpleNamespace(
            has=lambda n: True, load=lambda n: spec
        )
        fake = SimpleNamespace(exit_code=0)
        entry = _entry(id="e1")
        monkeypatch.setattr(
            agent_mod, "run_tool",
            lambda s, sp, i, sd, params=None, parent_id=None: (entry, fake),
        )
        out_entry, result = await agent.run_tool_with_logging(
            "t", {}, tmp_path
        )
        assert result is fake

    async def test_timeout_saves_and_raises(self, agent, tmp_path, monkeypatch):
        from bioledger.toolspec.models import (
            ExecutionMode,
            ExecutionSpec,
            ToolSpec,
        )

        spec = ToolSpec(
            execution=ExecutionSpec(
                name="t", command="c",
                suggested_mode=ExecutionMode.BLOCKING,
            )
        )
        agent.tool_store = SimpleNamespace(
            has=lambda n: True, load=lambda n: spec
        )

        def timeout(*a, **k):
            raise TimeoutError("timed out")

        monkeypatch.setattr(agent_mod, "run_tool", timeout)
        with pytest.raises(TimeoutError):
            await agent.run_tool_with_logging("t", {}, tmp_path)
        agent._test_store.save_session.assert_called()


class TestPollPendingJobs:
    def _wire(self, agent, tmp_path, monkeypatch):
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        monkeypatch.setattr(
            agent_mod, "get_session_dir", lambda h, s: session_dir
        )
        return session_dir

    def test_no_running_returns_empty(self, agent, tmp_path, monkeypatch):
        self._wire(agent, tmp_path, monkeypatch)
        assert agent.poll_pending_jobs() == []

    def test_docker_entry_uses_poll_tool(self, agent, tmp_path, monkeypatch):
        self._wire(agent, tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            agent_mod, "poll_tool", lambda e, s: calls.append(e.id)
        )
        agent.session.add(
            _entry(id="d1", run_status="running", executor="docker")
        )
        agent.poll_pending_jobs()
        assert calls == ["d1"]

    def test_engine_entry_missing_sidecar_fails(
        self, agent, tmp_path, monkeypatch
    ):
        self._wire(agent, tmp_path, monkeypatch)
        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda e, c: MagicMock(),
        )
        agent.session.add(
            _entry(id="e1", run_status="running", executor="nextflow")
        )
        done = agent.poll_pending_jobs()
        assert [e.id for e in done] == ["e1"]
        e = agent.session.entries[0]
        assert e.run_status == "failed"
        assert "sidecar lost" in e.notes

    def test_engine_status_none_bad_handle_fails(
        self, agent, tmp_path, monkeypatch
    ):
        sd = self._wire(agent, tmp_path, monkeypatch)
        agent_mod._write_engine_metadata_sidecar(
            sd, "e1",
            SimpleNamespace(
                work_dir=tmp_path, run_id="bad", executor="nextflow",
                submitted_at="t", status="running",
                process_start_time=None, metadata={},
            ),
        )
        adapter = MagicMock()
        adapter.poll.return_value = None
        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda e, c: adapter,
        )
        agent.session.add(
            _entry(id="e1", run_status="running", executor="nextflow",
                   run_handle_id="bad")
        )
        agent.poll_pending_jobs()
        assert agent.session.entries[0].run_status == "failed"
        assert "Invalid or missing run handle" in (
            agent.session.entries[0].notes
        )

    def test_engine_status_none_valid_handle_retries(
        self, agent, tmp_path, monkeypatch
    ):
        sd = self._wire(agent, tmp_path, monkeypatch)
        agent_mod._write_engine_metadata_sidecar(
            sd, "e1",
            SimpleNamespace(
                work_dir=tmp_path, run_id="1234", executor="nextflow",
                submitted_at="t", status="running",
                process_start_time=None, metadata={},
            ),
        )
        adapter = MagicMock()
        adapter.poll.return_value = None
        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda e, c: adapter,
        )
        agent.session.add(
            _entry(id="e1", run_status="running", executor="nextflow",
                   run_handle_id="1234")
        )
        assert agent.poll_pending_jobs() == []
        assert agent.session.entries[0].run_status == "running"

    def test_engine_terminal_finalizes(self, agent, tmp_path, monkeypatch):
        sd = self._wire(agent, tmp_path, monkeypatch)
        agent_mod._write_engine_metadata_sidecar(
            sd, "e1",
            SimpleNamespace(
                work_dir=tmp_path, run_id="9", executor="snakemake",
                submitted_at="t", status="running",
                process_start_time=None, metadata={},
            ),
        )
        adapter = MagicMock()
        adapter.poll.return_value = SimpleNamespace(
            state="completed", message=None
        )

        def fake_finalize(entry, rh, wd, ad, status):
            entry.run_status = "completed"
            entry.exit_code = 0

        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda e, c: adapter,
        )
        monkeypatch.setattr(
            agent_mod, "_finalize_engine_entry", fake_finalize
        )
        agent.session.add(
            _entry(id="e1", run_status="running", executor="snakemake",
                   run_handle_id="9")
        )
        done = agent.poll_pending_jobs()
        assert [e.id for e in done] == ["e1"]
        agent._test_store.save_session.assert_called()

    def test_engine_poll_exception_tolerated(
        self, agent, tmp_path, monkeypatch
    ):
        sd = self._wire(agent, tmp_path, monkeypatch)
        agent_mod._write_engine_metadata_sidecar(
            sd, "e1",
            SimpleNamespace(
                work_dir=tmp_path, run_id="9", executor="nextflow",
                submitted_at="t", status="running",
                process_start_time=None, metadata={},
            ),
        )
        adapter = MagicMock()
        adapter.poll.side_effect = RuntimeError("poll boom")
        monkeypatch.setattr(
            "bioledger.core.engines.dispatch._get_engine_adapter",
            lambda e, c: adapter,
        )
        agent.session.add(
            _entry(id="e1", run_status="running", executor="nextflow",
                   run_handle_id="9")
        )
        assert agent.poll_pending_jobs() == []
        assert agent.session.entries[0].run_status == "running"
