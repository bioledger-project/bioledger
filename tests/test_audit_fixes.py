"""Regression tests for the post-Phase-5 codebase audit fixes.

Covers: docker-absent import safety, _render_command output refs +
required-input validation, snakemake {{ outputs.X.path }} rendering,
staged-path input FileRefs, crystallize glob-output fallback, Tool Shed
<collection> outputs, and corrupt registry caches.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def _docker_spec(**over) -> ToolSpec:
    defaults = dict(
        name="sorter",
        container="img:1",
        executors=["docker"],
        command="samtools sort -o {{ outputs.sorted.path }} {{ inputs.bam }}",
        inputs={"bam": ToolInput(type=ParamType.FILE, format="bam")},
        outputs={"sorted": ToolOutput(format="bam", pattern="sorted.bam")},
        status=SpecStatus.VALID,
    )
    defaults.update(over)
    return ToolSpec(execution=ExecutionSpec(**defaults))


class TestDockerAbsentImportSafety:
    """Phase 5 gate: CLI must import when docker is absent/blocked."""

    def test_cli_imports_without_docker(self):
        code = (
            "import sys; sys.modules['docker'] = None; "
            "import bioledger.apps.cli.main; print('ok')"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr

    def test_executor_imports_without_docker(self):
        code = (
            "import sys; sys.modules['docker'] = None; "
            "import bioledger.forges.analysisforge.executor; print('ok')"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr


class TestRenderCommandOutputs:
    def test_bare_outputs_ref_renders_path(self):
        """{{ outputs.X }} must render the path, not a dict repr."""
        from bioledger.forges.analysisforge.executor import _render_command

        spec = _docker_spec(command="cp {{ outputs.sorted }} /dest/")
        rendered = _render_command(spec, {"bam": "in.bam"}, {})
        assert rendered == "cp /work/sorted.bam /dest/"
        assert "{" not in rendered

    def test_outputs_path_attr_renders_path(self):
        from bioledger.forges.analysisforge.executor import _render_command

        spec = _docker_spec()
        rendered = _render_command(spec, {"bam": "in.bam"}, {})
        assert rendered == "samtools sort -o /work/sorted.bam /work/in.bam"

    def test_missing_required_input_raises(self):
        from bioledger.forges.analysisforge.executor import _render_command

        spec = _docker_spec()
        with pytest.raises(ValueError, match="Missing required input"):
            _render_command(spec, {}, {})

    def test_absent_optional_input_renders_empty(self):
        from bioledger.forges.analysisforge.executor import _render_command

        spec = _docker_spec(
            command="tool -i {{ inputs.bam }} {% if inputs.idx %}-x {{ inputs.idx }}{% endif %}",
            inputs={
                "bam": ToolInput(type=ParamType.FILE, format="bam"),
                "idx": ToolInput(
                    type=ParamType.FILE, format="bai", required=False
                ),
            },
        )
        rendered = _render_command(spec, {"bam": "in.bam"}, {})
        assert rendered == "tool -i /work/in.bam "


class TestSnakefileOutputPathRef:
    """{{ outputs.X.path }} in a toolspec must render {output.X} in the
    generated Snakefile — a plain str ctx value silently rendered ''."""

    class _I:
        def __init__(self, collection=False):
            self.collection = collection

    class _O:
        def __init__(self, pattern):
            self.pattern = pattern

    class _Spec:
        name = "samtools-sort"
        inputs = {"bam": None}
        outputs = {"sorted": None}
        parameters = {}

    def test_outputs_path_renders_output_placeholder(self, tmp_path):
        from bioledger.core.engines.snakemake_adapter import _generate_snakefile

        spec = self._Spec()
        spec.command = "samtools sort -o {{ outputs.sorted.path }} {{ inputs.bam }}"
        spec.inputs = {"bam": self._I(collection=True)}
        spec.outputs = {"sorted": self._O("{sample}.sorted.bam")}
        _generate_snakefile(
            spec, {"bam": "data/*.bam"}, "img:1", tmp_path / "Snakefile"
        )
        sf = (tmp_path / "Snakefile").read_text()
        assert "-o {output.sorted}" in sf
        # The old bug rendered `-o  {input.bam}` — the flag swallowing the
        # next arg because .path on a str evaluated to "".
        assert "-o  " not in sf


class TestFanoutCollisionStaging:
    """Two inputs with the same basename must keep collision-renamed
    staged names consistent across input_mapping, config, and FileRefs."""

    def _make_agent(self, tmp_path: Path):
        from bioledger.config import BioLedgerConfig
        from bioledger.forges.analysisforge.agent import AnalysisForgeAgent
        from bioledger.ledger.store import LedgerStore
        from bioledger.toolspec.store import ToolStore

        spec = _docker_spec(
            inputs={
                "r1": ToolInput(type=ParamType.FILE, format="fastq"),
                "r2": ToolInput(type=ParamType.FILE, format="fastq"),
            },
        )
        config = BioLedgerConfig(home_dir=str(tmp_path))
        tool_store = MagicMock(spec=ToolStore)
        tool_store.has.return_value = True
        tool_store.load.return_value = spec
        agent = object.__new__(AnalysisForgeAgent)
        agent.config = config
        agent.tool_store = tool_store
        agent.store = MagicMock(spec=LedgerStore)
        agent.session = LedgerSession(name="test")
        agent._tool_library = {}
        return agent

    def test_colliding_basenames_use_staged_names(self, tmp_path):
        from bioledger.core.engines.protocol import RunHandle, WorkflowHandle

        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "reads.fq").write_text("@r1\nACGT\n+\nIIII\n")
        (d2 / "reads.fq").write_text("@r2\nTGCA\n+\nIIII\n")

        agent = self._make_agent(tmp_path)
        mock_handle = WorkflowHandle(
            executor="snakemake",
            artifact_path=tmp_path / "test.smk",
            step_inventory=["sorter"],
            container_image="img:1",
            input_mapping={},
        )
        mock_run_handle = RunHandle(
            executor="snakemake", run_id="42", entry_id="", work_dir=tmp_path
        )
        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = mock_handle
        mock_adapter.submit.return_value = mock_run_handle

        with (
            patch(
                "bioledger.core.engines.dispatch.resolve_engine_for_fanout",
                return_value="snakemake",
            ),
            patch(
                "bioledger.core.engines.dispatch._get_engine_adapter",
                return_value=mock_adapter,
            ),
        ):
            entry, _ = agent.submit_fanout(
                tool_name="sorter",
                input_mapping={
                    "r1": str(d1 / "reads.fq"),
                    "r2": str(d2 / "reads.fq"),
                },
                params={},
            )

        fanout_dir = Path(agent.config.home_dir) / "sessions" / agent.session.id / f"fanout_{entry.id}"
        # The mapping handed to the engine (and persisted on the entry)
        # must use the staged names — the files that actually exist.
        submitted = mock_adapter.submit.call_args.kwargs["input_mapping"]
        assert submitted["r1"] == "r1__reads.fq"
        assert submitted["r2"] == "r2__reads.fq"
        assert (fanout_dir / "r1__reads.fq").exists()
        assert (fanout_dir / "r2__reads.fq").exists()
        assert entry.container.input_mapping == submitted

        # Input FileRefs must carry STAGED paths — build_rocrate stages
        # Path(f.path).name, which must equal the input_mapping basename
        # for Channel.fromPath targets to exist in the crate.
        input_refs = {Path(f.path).name: f for f in entry.files if f.role == "input"}
        assert set(input_refs) == {"r1__reads.fq", "r2__reads.fq"}
        for name, ref in input_refs.items():
            assert Path(ref.path).parent == fanout_dir


class TestCrystallizeGlobFallback:
    def test_unrecorded_glob_output_falls_back_to_matching_name(self):
        """With no recorded outputs, the rendered filename must still
        satisfy the declared `path '<glob>', emit:` declaration."""
        from bioledger.forges.analysisforge.crystallize import (
            _output_name_to_filename,
        )

        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_snapshot={
                "outputs": {"sorted": {"pattern": "*.sorted.bam"}},
            },
            files=[],
        )
        names = _output_name_to_filename(entry)
        assert fnmatch.fnmatch(names["sorted"], "*.sorted.bam")


class TestToolShedCollectionOutputs:
    def test_collection_output_parsed(self, tmp_path):
        from bioledger.core.engines.toolshed_search import parse_tool_to_spec

        (tmp_path / "tool.xml").write_text(
            """<tool id="rep" name="rep" version="1.0">
  <command>run.sh</command>
  <inputs><param name="x" type="data" format="txt"/></inputs>
  <outputs>
    <data name="out" format="txt" from_work_dir="out.txt"/>
    <collection name="report_files" type="list" format="html"/>
  </outputs>
</tool>"""
        )
        spec = parse_tool_to_spec(tmp_path)
        assert spec.outputs["out"].pattern == "out.txt"
        assert spec.outputs["report_files"].pattern == "report_files/"


class TestRegistryCache:
    def test_corrupt_cache_treated_as_miss(self, tmp_path, monkeypatch):
        from bioledger.forges.toolforge import registry

        monkeypatch.setattr(registry, "_CACHE_DIR", tmp_path)
        (tmp_path / "borked.json").write_text("{not json")
        assert registry._read_cache("borked") is None
