"""Tests for the small zero-coverage modules:
forge base class, LLM tracking, ToolForgeAgent, ISAForge agent factory,
analysisforge suggest helpers, and the Nextflow translator.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bioledger.config import BioLedgerConfig
from bioledger.core.forge import Forge
from bioledger.core.llm.tracking import log_llm_result
from bioledger.forges.analysisforge import suggest
from bioledger.forges.analysisforge.suggest import (
    suggest_analysis_for_dataset,
    suggest_tools_for_workflow,
    suggest_workflow_for_dataset,
)
from bioledger.forges.isaforge import agent as isa_agent_mod
from bioledger.forges.isaforge.agent import make_isa_agent
from bioledger.forges.isaforge.dataset import DataFile, DataSet
from bioledger.forges.toolforge import agent as tf_agent_mod
from bioledger.forges.toolforge.agent import ToolForgeAgent
from bioledger.forges.toolforge.translators.nextflow import (
    to_nextflow_process,
    validate_nextflow_dsl2,
)
from bioledger.ledger.models import EntryKind, LedgerSession
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    ToolParameter,
)
from bioledger.toolspec.validate import Severity, ValidationIssue


class TestForgeBase:
    def test_cannot_instantiate_abstract(self, sample_tool_spec):
        with pytest.raises(TypeError):
            Forge(BioLedgerConfig(), LedgerSession(name="s"), MagicMock())

    def test_subclass_stores_dependencies(self):
        class MyForge(Forge):
            @property
            def name(self):
                return "myforge"

            async def initialize(self):
                return None

        config = BioLedgerConfig()
        session = LedgerSession(name="s")
        store = MagicMock()
        forge = MyForge(config, session, store)
        assert forge.config is config
        assert forge.session is session
        assert forge.store is store
        assert forge.name == "myforge"


class _Msg:
    """Fake pydantic-ai message."""

    def __init__(self, parts=(), usage=None):
        self.parts = list(parts)
        self.usage = usage

    def model_dump(self):
        return {"fake": "msg"}


class TestLogLlmResult:
    def _result(self, messages, output="done", model="m-1"):
        return SimpleNamespace(
            all_messages=lambda: messages,
            output=output,
            _model_name=model,
        )

    def test_logs_entry_with_messages_and_tokens(self):
        session = LedgerSession(name="s")
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        tool_part = SimpleNamespace(tool_name="lookup")
        other_part = SimpleNamespace(text="hi")
        msgs = [
            _Msg(parts=[tool_part], usage=usage),
            _Msg(parts=[other_part], usage=None),
        ]
        entry = log_llm_result(session, self._result(msgs))
        assert entry.kind == EntryKind.LLM_CALL
        info = entry.llm_call
        assert info.model == "m-1"
        assert info.tokens_used == 15
        assert info.tool_calls == ["lookup"]
        assert info.output_summary == "done"
        assert len(info.full_messages) == 2
        assert session.entries[0] is entry

    def test_empty_messages_defaults(self):
        session = LedgerSession(name="s")
        entry = log_llm_result(
            session, self._result([], output="o" * 600)
        )
        assert entry.llm_call.tokens_used == 0
        assert entry.llm_call.prompt_summary == ""
        assert len(entry.llm_call.output_summary) == 500

    def test_parent_id_and_unknown_model(self):
        session = LedgerSession(name="s")
        result = SimpleNamespace(
            all_messages=lambda: [], output="x"
        )  # no _model_name attr
        entry = log_llm_result(session, result, parent_id="p1")
        assert entry.parent_id == "p1"
        assert entry.llm_call.model == "unknown"


class TestToolForgeAgent:
    def _agent_with(self, output):
        agent = MagicMock()
        agent.run = MagicMock(return_value=None)

        async def run(prompt, deps=None):
            agent.last_prompt = prompt
            return SimpleNamespace(output=output)

        agent.run = run
        return agent

    def test_agents_lazily_created_and_cached(self):
        made = []
        with patch.object(
            tf_agent_mod, "make_agent",
            side_effect=lambda cfg, **kw: made.append(kw) or MagicMock(),
        ):
            tf = ToolForgeAgent(BioLedgerConfig())
            a1 = tf._parse_agent
            a2 = tf._parse_agent
        assert a1 is a2
        assert len(made) == 1
        assert made[0]["task"] == "parse_fallback"

    async def test_parse_calls_agent_and_converts(self):
        spec = ExecutionSpec(name="t", command="t {{inputs.x}}")
        draft = MagicMock()
        draft.to_execution_spec.return_value = spec
        agent = self._agent_with(draft)
        with patch.object(tf_agent_mod, "make_agent", return_value=agent):
            out = await ToolForgeAgent(BioLedgerConfig()).parse(
                "src-text", "toolshed", "parse err", deps=MagicMock()
            )
        assert out is spec
        assert "parse err" in agent.last_prompt
        assert "src-text" in agent.last_prompt

    async def test_fix_returns_validated_spec(self, sample_exec_spec):
        draft = MagicMock()
        draft.to_execution_spec.return_value = sample_exec_spec
        agent = self._agent_with(draft)
        issues = [ValidationIssue(
            field="f", message="m", severity=Severity.ERROR
        )]
        with patch.object(tf_agent_mod, "make_agent", return_value=agent):
            out = await ToolForgeAgent(BioLedgerConfig()).fix(
                sample_exec_spec, issues, deps=MagicMock()
            )
        assert out is sample_exec_spec
        assert "- f: m" in agent.last_prompt

    async def test_fix_raises_when_spec_still_invalid(self):
        bad = ExecutionSpec(name="t", command="cat {{inputs.missing}}")
        draft = MagicMock()
        draft.to_execution_spec.return_value = bad
        agent = self._agent_with(draft)
        with patch.object(tf_agent_mod, "make_agent", return_value=agent):
            with pytest.raises(ValueError, match="still invalid|invalid"):
                await ToolForgeAgent(BioLedgerConfig()).fix(
                    bad, [], deps=MagicMock()
                )

    async def test_review_returns_issue_list(self, sample_exec_spec):
        agent = self._agent_with(["looks odd"])
        with patch.object(tf_agent_mod, "make_agent", return_value=agent):
            out = await ToolForgeAgent(BioLedgerConfig()).review(
                sample_exec_spec, "toolshed", deps=MagicMock()
            )
        assert out == ["looks odd"]


class TestMakeIsaAgent:
    async def test_tool_uses_ontology_lookup(self):
        real_agent = MagicMock()
        registered = {}

        def tool_decorator(fn):
            registered[fn.__name__] = fn
            return fn

        real_agent.tool = tool_decorator
        with (
            patch.object(
                isa_agent_mod, "make_agent", return_value=real_agent
            ),
            patch.object(
                isa_agent_mod, "search_ontology"
            ) as mock_search,
        ):
            mock_search.return_value = [
                {"label": "T1", "iri": "http://i/1"},
                {"label": "T2", "iri": "http://i/2"},
            ]
            agent = make_isa_agent(BioLedgerConfig())
            out = await registered["lookup_ontology_term"](
                MagicMock(), "query", ontology="efo"
            )
        assert agent is real_agent
        mock_search.assert_awaited_once()
        assert "T1 (http://i/1)" in out

    async def test_tool_no_results_message(self):
        registered = {}
        real_agent = MagicMock()
        real_agent.tool = lambda fn: registered.setdefault(
            fn.__name__, fn
        ) or fn
        with (
            patch.object(
                isa_agent_mod, "make_agent", return_value=real_agent
            ),
            patch.object(
                isa_agent_mod, "search_ontology", return_value=[]
            ),
        ):
            make_isa_agent(BioLedgerConfig())
            out = await registered["lookup_ontology_term"](
                MagicMock(), "zzz", ontology="obi"
            )
        assert "No terms found for 'zzz' in obi" in out


def _dataset(**kw):
    defaults = dict(
        name="ds",
        description="desc",
        files=[DataFile(location="a.fastq", format="fastq")],
        file_formats=["fastq"],
        organisms=["Homo sapiens"],
        assay_type="rna-seq",
        sample_metadata={"s1": {}},
    )
    defaults.update(kw)
    return DataSet(**defaults)


class TestSuggest:
    def _fake_agent(self, output):
        agent = MagicMock()

        async def run(prompt, deps=None):
            agent.last_prompt = prompt
            return SimpleNamespace(output=output)

        agent.run = run
        return agent

    async def test_workflow_suggestion_prompt_and_output(self):
        wf = suggest.WorkflowSuggestion(
            analysis_steps=["QC"],
            rationale="because",
            user_confirmation_prompt="ok?",
        )
        agent = self._fake_agent(wf)
        with patch.object(
            suggest, "make_agent", return_value=agent
        ):
            out = await suggest_workflow_for_dataset(
                _dataset(), user_goal="find genes"
            )
        assert out.analysis_steps == ["QC"]
        assert "rna-seq" in agent.last_prompt
        assert "find genes" in agent.last_prompt
        assert "Homo sapiens" in agent.last_prompt

    async def test_tools_for_workflow_uses_store(self):
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(name="fastqc"),
            SimpleNamespace(name="multiqc"),
        ]
        agent = self._fake_agent(
            ["fastqc", "multiqc", "x", "y", "z", "w", "v"]
        )
        with (
            patch.object(suggest, "make_agent", return_value=agent),
            patch.object(suggest, "ToolStore", return_value=store),
        ):
            out = await suggest_tools_for_workflow(
                _dataset(),
                suggest.WorkflowSuggestion(
                    analysis_steps=["QC", "Align"],
                    rationale="r",
                    user_confirmation_prompt="p",
                ),
            )
        store.search.assert_called_with(input_format="fastq")
        # trimmed to 5 per step
        assert out["QC"] == ["fastqc", "multiqc", "x", "y", "z"]
        assert "Align" in out

    async def test_full_suggestion_shape(self):
        wf = suggest.WorkflowSuggestion(
            analysis_steps=["QC"],
            rationale="r",
            user_confirmation_prompt="p",
            alternative_approaches=["alt"],
        )
        agent = MagicMock()

        async def run(prompt, deps=None):
            if "workflow" in prompt.lower() or "Dataset:" in prompt:
                return SimpleNamespace(output=wf)
            return SimpleNamespace(output=["fastqc"])

        agent.run = run
        store = MagicMock()
        store.search.return_value = [SimpleNamespace(name="fastqc")]
        with (
            patch.object(suggest, "make_agent", return_value=agent),
            patch.object(suggest, "ToolStore", return_value=store),
        ):
            out = await suggest_analysis_for_dataset(_dataset())
        assert out["workflow"] == ["QC"]
        assert out["rationale"] == "r"
        assert out["tools_by_step"] == {"QC": ["fastqc"]}
        assert out["alternative_approaches"] == ["alt"]
        summary = out["dataset_summary"]
        assert summary["name"] == "ds"
        assert summary["formats"] == ["fastq"]
        assert summary["file_count"] == 1
        assert summary["remote_file_count"] == 0


class TestNextflowTranslator:
    def test_process_structure(self, sample_exec_spec):
        nf = to_nextflow_process(sample_exec_spec)
        assert "process fastqc {" in nf
        assert "container 'quay.io/biocontainers/fastqc:0.11.9--0'" in nf
        assert "path reads" in nf
        assert "val threads" in nf
        assert 'path "*_fastqc.html", emit: report' in nf
        # Jinja parameter refs rewritten to NF variables
        assert "${threads}" in nf
        assert "{{parameters.threads}}" not in nf
        assert "params.threads = 4" in nf
        assert validate_nextflow_dsl2(nf) == []

    def test_param_defaults_by_type(self):
        spec = ExecutionSpec(
            name="t",
            command="x",
            parameters={
                "i": ToolParameter(type=ParamType.INTEGER),
                "b": ToolParameter(type=ParamType.BOOLEAN),
                "s": ToolParameter(type=ParamType.STRING),
            },
        )
        nf = to_nextflow_process(spec)
        assert "params.i = 0" in nf
        assert "params.b = false" in nf
        assert "params.s = ''" in nf

    def test_no_container_line_when_empty(self):
        spec = ExecutionSpec(name="t", command="x", container="")
        nf = to_nextflow_process(spec)
        assert "container '" not in nf
        assert "Missing container directive" in " ".join(
            validate_nextflow_dsl2(nf)
        )

    def test_output_pattern_fallback_to_format(self):
        from bioledger.toolspec.models import ToolOutput

        spec = ExecutionSpec(
            name="t",
            command="x",
            outputs={"o": ToolOutput(format="bam")},
        )
        nf = to_nextflow_process(spec)
        assert 'path "*.bam", emit: o' in nf


class TestValidateNextflowDsl2:
    def test_all_issues(self):
        issues = validate_nextflow_dsl2("nothing here {")
        joined = " ".join(issues)
        assert "No process block" in joined
        assert "Missing container" in joined
        assert "Missing input:" in joined
        assert "Missing output:" in joined
        assert "Missing script:" in joined
        assert "Unbalanced braces" in joined

    def test_valid_process_no_issues(self):
        nf = (
            "process T {\n container 'c'\n input:\n x\n"
            " output:\n y\n script:\n \"\"\"\n run\n \"\"\"\n}"
        )
        assert validate_nextflow_dsl2(nf) == []


def test_reexport_shims_importable():
    """The 1-line re-export shims resolve to the schema package."""
    import bioledger.toolspec.containers as c
    import bioledger.toolspec.sources as s

    assert hasattr(c, "resolve_most_recent_version")
    assert hasattr(s, "LocalDirSource")
