"""Tests for ISAForge builder: investigation construction, CSV→ISA-Tab
conversion (heuristic and LLM-assisted paths), and LLM helper agents."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from isatools.model import Investigation

from bioledger.config import BioLedgerConfig
from bioledger.forges.isaforge import builder
from bioledger.forges.isaforge.builder import (
    _llm_assay_inference,
    _llm_organism_lookup,
    _make_organism_agent,
    create_investigation,
    csv_to_isatab,
    write_isatab,
)
from bioledger_isatab_schema.models import (
    CharacteristicSpec,
    ISAStudySpec,
    OntologySourceSpec,
    SampleSpec,
    SourceSpec,
)


def _spec(**kw) -> ISAStudySpec:
    defaults = dict(
        investigation_id="inv1",
        title="My Investigation",
        description="desc",
        study_id="s1",
        study_title="My Study",
        study_description="sd",
    )
    defaults.update(kw)
    return ISAStudySpec(**defaults)


class TestCreateInvestigation:
    def test_minimal_spec(self):
        inv = create_investigation(_spec())
        assert isinstance(inv, Investigation)
        assert inv.identifier == "inv1"
        assert inv.title == "My Investigation"
        assert len(inv.studies) == 1
        assert inv.studies[0].identifier == "s1"

    def test_ontology_sources_mapped(self):
        inv = create_investigation(
            _spec(
                ontology_sources=[
                    OntologySourceSpec(name="OBI", file="f", version="1"),
                    OntologySourceSpec(name="NCBITaxon"),
                ]
            )
        )
        names = [s.name for s in inv.ontology_source_references]
        assert names == ["OBI", "NCBITaxon"]

    def test_sources_get_organism_characteristics(self):
        inv = create_investigation(
            _spec(
                sources=[
                    SourceSpec(
                        name="src1",
                        characteristics=[CharacteristicSpec(term="Homo sapiens")],
                    )
                ],
                samples=[SampleSpec(name="smp1")],
            )
        )
        study = inv.studies[0]
        assert study.sources[0].name == "src1"
        char = study.sources[0].characteristics[0]
        # category must be a Characteristic(category=OntologyAnnotation)
        # so isatools emits a Characteristics[] column
        assert char.category.term == "Organism"
        assert char.value.term == "Homo sapiens"
        assert study.samples[0].name == "smp1"


class TestWriteIsatab:
    def test_writes_files(self, tmp_path):
        inv = create_investigation(_spec())
        out = write_isatab(inv, tmp_path / "isa")
        assert list(out.glob("i_*.txt"))


class _FakeAgent:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    async def run(self, prompt, deps=None):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


def _csv(tmp_path, rows, header="sample,organism,file"):
    p = tmp_path / "samples.csv"
    p.write_text(header + "\n" + "\n".join(rows) + "\n")
    return p


class TestCsvToIsatab:
    async def test_heuristic_path_no_config(self, tmp_path):
        csv = _csv(
            tmp_path,
            ["s1,Homo sapiens,a.fastq", "s2,Mus musculus,b.fastq"],
        )
        out = await csv_to_isatab(csv, tmp_path / "isa")
        assert (out / "samples.csv").exists()  # CSV copied alongside
        assert (out / "s_study.txt").exists()
        assert (out / "a_assay.txt").exists()
        # investigation file exists
        assert list(out.glob("i_*.txt"))

    async def test_heuristic_format_hints(self, tmp_path, monkeypatch):
        csv = _csv(tmp_path, ["s1,H. sapiens,a.vcf"])
        dumped = {}

        def fake_dump(inv, path):
            dumped["inv"] = inv
            import os

            os.makedirs(path, exist_ok=True)

        monkeypatch.setattr(builder.isatab, "dump", fake_dump)
        await csv_to_isatab(csv, tmp_path / "isa")
        assay = dumped["inv"].studies[0].assays[0]
        # fastq→sequencing hint map; vcf→sequence variation analysis
        assert assay.technology_type.term == "sequence variation analysis"
        assert assay.measurement_type.term == "sample analysis"

    async def test_llm_assay_inference_used_with_config(
        self, tmp_path, monkeypatch
    ):
        csv = _csv(tmp_path, ["s1,H. sapiens,a.fastq"])
        calls = []

        async def fake_infer(formats, config):
            calls.append(formats)
            return {
                "measurement_type": "transcription profiling",
                "technology_type": "nucleotide sequencing",
            }

        dumped = {}

        def fake_dump(inv, path):
            dumped["inv"] = inv
            import os

            os.makedirs(path, exist_ok=True)

        monkeypatch.setattr(builder.isatab, "dump", fake_dump)
        monkeypatch.setattr(builder, "_llm_assay_inference", fake_infer)
        monkeypatch.setattr(
            builder,
            "_make_organism_agent",
            lambda cfg: (MagicMock(), MagicMock()),
        )

        async def fake_org(raw, agent, deps):
            return "resolved:" + raw

        monkeypatch.setattr(builder, "_llm_organism_lookup", fake_org)
        await csv_to_isatab(csv, tmp_path / "isa", config=BioLedgerConfig())
        assert calls == [["fastq"]]
        assay = dumped["inv"].studies[0].assays[0]
        assert assay.measurement_type.term == "transcription profiling"
        char = dumped["inv"].studies[0].sources[0].characteristics[0]
        assert char.value.term == "resolved:H. sapiens"

    async def test_organism_lookup_dedupes_and_falls_back(
        self, tmp_path, monkeypatch
    ):
        csv = _csv(
            tmp_path,
            [
                "s1,Homo sapiens,a.fastq",
                "s2,Homo sapiens,b.fastq",
                "s3,Mus musculus,c.fastq",
            ],
        )
        lookups = []

        async def fake_org(raw, agent, deps):
            lookups.append(raw)
            if raw == "Mus musculus":
                raise RuntimeError("llm down")
            return raw.upper()

        monkeypatch.setattr(
            builder,
            "_make_organism_agent",
            lambda cfg: (MagicMock(), MagicMock()),
        )
        monkeypatch.setattr(builder, "_llm_organism_lookup", fake_org)

        async def fake_infer(formats, config):
            return {"measurement_type": "m", "technology_type": "t"}

        monkeypatch.setattr(builder, "_llm_assay_inference", fake_infer)
        dumped = {}
        monkeypatch.setattr(
            builder.isatab,
            "dump",
            lambda inv, path: dumped.setdefault("inv", inv)
            or __import__("os").makedirs(path, exist_ok=True),
        )
        await csv_to_isatab(csv, tmp_path / "isa", config=BioLedgerConfig())
        # unique organisms looked up once each
        assert lookups == ["Homo sapiens", "Mus musculus"]
        chars = {
            s.name: s.characteristics[0].value.term
            for s in dumped["inv"].studies[0].sources
        }
        assert chars["source_s1"] == "HOMO SAPIENS"
        # LLM failure falls back to raw value
        assert chars["source_s3"] == "Mus musculus"

    async def test_missing_sample_name_defaults(self, tmp_path, monkeypatch):
        csv = _csv(tmp_path, [",H. sapiens,a.fastq"])
        dumped = {}
        monkeypatch.setattr(
            builder.isatab,
            "dump",
            lambda inv, path: dumped.setdefault("inv", inv)
            or __import__("os").makedirs(path, exist_ok=True),
        )
        await csv_to_isatab(csv, tmp_path / "isa")
        samples = dumped["inv"].studies[0].samples
        assert samples[0].name == "sample_0"

    async def test_dedupes_data_files_but_keeps_processes(
        self, tmp_path, monkeypatch
    ):
        csv = _csv(
            tmp_path,
            ["s1,H,a.fastq", "s2,M,a.fastq"],  # same file both rows
        )
        dumped = {}
        monkeypatch.setattr(
            builder.isatab,
            "dump",
            lambda inv, path: dumped.setdefault("inv", inv)
            or __import__("os").makedirs(path, exist_ok=True),
        )
        await csv_to_isatab(csv, tmp_path / "isa")
        assay = dumped["inv"].studies[0].assays[0]
        assert len(assay.data_files) == 1
        assert len(assay.process_sequence) == 2


class TestOrganismAgent:
    async def test_agent_tool_and_deps(self):
        registered = {}
        fake = MagicMock()
        fake.tool = lambda fn: registered.setdefault(fn.__name__, fn) or fn
        with (
            patch.object(builder, "make_agent", return_value=fake),
            patch.object(builder, "search_ontology") as mock_search,
        ):
            mock_search.return_value = [{"label": "L", "iri": "i"}]
            agent, deps = _make_organism_agent(BioLedgerConfig())
            out = await registered["search_ontology_tool"](MagicMock(), "q")
        assert agent is fake
        assert deps.config is not None
        mock_search.assert_awaited_once()
        assert "L (i)" in out

    async def test_tool_no_results(self):
        registered = {}
        fake = MagicMock()
        fake.tool = lambda fn: registered.setdefault(fn.__name__, fn) or fn
        with (
            patch.object(builder, "make_agent", return_value=fake),
            patch.object(builder, "search_ontology", return_value=[]),
        ):
            _make_organism_agent(BioLedgerConfig())
            out = await registered["search_ontology_tool"](
                MagicMock(), "q", ontology="efo"
            )
        assert "No results for 'q' in efo" in out


class TestLlmHelpers:
    async def test_organism_lookup_returns_label(self):
        agent = _FakeAgent(SimpleNamespace(organism_label="Homo sapiens"))
        out = await _llm_organism_lookup("human", agent, MagicMock())
        assert out == "Homo sapiens"
        assert "'human'" in agent.prompts[0]

    async def test_assay_inference_returns_dict(self):
        agent = _FakeAgent(
            SimpleNamespace(
                measurement_type="m", technology_type="t", reasoning="r"
            )
        )
        with patch.object(builder, "make_agent", return_value=agent):
            out = await _llm_assay_inference(["fastq"], BioLedgerConfig())
        assert out == {"measurement_type": "m", "technology_type": "t"}
        assert "fastq" in agent.prompts[0]
