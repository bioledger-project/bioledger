"""Regression tests for AnalysisForge CLI input resolution.

Covers the bug where a derived sidecar file (e.g. a samtools '<ref>.dict')
whose name contains an earlier file's exact name as a substring would
shadow the real file, because prior-run outputs were matched by substring
before dataset/datasets-dir exact-name lookups ever ran.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bioledger.apps.cli.main import _resolve_inputs
from bioledger.forges.analysisforge.agent import KeyValuePair, ToolRunRequest
from bioledger.forges.isaforge.dataset import DataFile, DataSet
from bioledger.ledger.models import EntryKind, FileRef, LedgerEntry, LedgerSession


def _make_tool_request(tool_name: str, mapping: dict[str, str]) -> ToolRunRequest:
    return ToolRunRequest(
        tool_name=tool_name,
        rationale="test",
        suggested_params=[],
        input_mapping=[KeyValuePair(key=k, value=v) for k, v in mapping.items()],
    )


def _make_agent(*, dataset: DataSet | None, home_dir: Path, session: LedgerSession):
    """Minimal duck-typed stand-in for AnalysisForgeAgent.

    _resolve_inputs only touches .session, .dataset, .config.home_dir, and
    .tool_store.load(...) — so a real AnalysisForgeAgent (which requires LLM
    config) isn't needed here.
    """
    tool_store = SimpleNamespace(load=lambda name: (_ for _ in ()).throw(KeyError(name)))
    config = SimpleNamespace(home_dir=home_dir)
    return SimpleNamespace(session=session, dataset=dataset, config=config, tool_store=tool_store)


class TestResolveInputsExactBeforeFuzzy:
    def test_prefers_exact_dataset_match_over_substring_prior_output(self, tmp_path: Path):
        """A '<ref>.dict' prior-run output must not shadow the real reference
        fasta when the real fasta is resolvable by exact name from the dataset."""
        ref_fasta = tmp_path / "GCF_000227135.1_ASM22713v2_genomic.fna.gz"
        ref_fasta.write_bytes(b"fake-fasta")

        dict_sidecar = tmp_path / "runs" / "run1" / "GCF_000227135.1_ASM22713v2_genomic.fna.gz.dict"
        dict_sidecar.parent.mkdir(parents=True)
        dict_sidecar.write_text("fake-dict")

        session = LedgerSession()
        session.entries.append(
            LedgerEntry(
                kind=EntryKind.TOOL_RUN,
                tool_spec_name="samtools-dict",
                files=[
                    FileRef(path=str(dict_sidecar), sha256="x", size_bytes=1, role="output"),
                ],
            )
        )

        dataset = DataSet(
            name="test-dataset",
            files=[DataFile(location=str(ref_fasta), format="fasta")],
        )

        agent = _make_agent(dataset=dataset, home_dir=tmp_path / "home", session=session)
        tool_request = _make_tool_request(
            "bwa-mem2-mem", {"ref_fasta": "GCF_000227135.1_ASM22713v2_genomic.fna.gz"}
        )

        input_files, parent_id = _resolve_inputs(agent, tool_request)

        assert input_files["ref_fasta"] == ref_fasta
        assert parent_id is None

    def test_prefers_exact_datasets_dir_match_over_substring_prior_output(self, tmp_path: Path):
        """Same scenario, but the real fasta only lives under
        ~/.bioledger/datasets/ (no loaded dataset) — exact match there must
        still win over the substring-matching '.dict' sidecar."""
        home_dir = tmp_path / "home"
        ref_fasta = home_dir / "datasets" / "GCF_1" / "GCF_000227135.1_ASM22713v2_genomic.fna.gz"
        ref_fasta.parent.mkdir(parents=True)
        ref_fasta.write_bytes(b"fake-fasta")

        dict_sidecar = tmp_path / "runs" / "run1" / "GCF_000227135.1_ASM22713v2_genomic.fna.gz.dict"
        dict_sidecar.parent.mkdir(parents=True)
        dict_sidecar.write_text("fake-dict")

        session = LedgerSession()
        session.entries.append(
            LedgerEntry(
                kind=EntryKind.TOOL_RUN,
                tool_spec_name="samtools-dict",
                files=[
                    FileRef(path=str(dict_sidecar), sha256="x", size_bytes=1, role="output"),
                ],
            )
        )

        agent = _make_agent(dataset=None, home_dir=home_dir, session=session)
        tool_request = _make_tool_request(
            "bwa-mem2-mem", {"ref_fasta": "GCF_000227135.1_ASM22713v2_genomic.fna.gz"}
        )

        input_files, parent_id = _resolve_inputs(agent, tool_request)

        assert input_files["ref_fasta"] == ref_fasta
        assert parent_id is None

    def test_exact_prior_output_match_still_resolves_and_sets_parent_id(self, tmp_path: Path):
        """Guard against regressing the intended chaining behavior: an exact
        filename match against a prior tool-run output should still resolve
        and record parent_id."""
        aligned_sam = tmp_path / "runs" / "run1" / "aligned.sam"
        aligned_sam.parent.mkdir(parents=True)
        aligned_sam.write_text("fake-sam")

        session = LedgerSession()
        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name="bwa-mem2-mem",
            files=[
                FileRef(path=str(aligned_sam), sha256="x", size_bytes=1, role="output"),
            ],
        )
        session.entries.append(entry)

        agent = _make_agent(dataset=None, home_dir=tmp_path / "home", session=session)
        tool_request = _make_tool_request("samtools-sort", {"in_sam": "aligned.sam"})

        input_files, parent_id = _resolve_inputs(agent, tool_request)

        assert input_files["in_sam"] == aligned_sam
        assert parent_id == entry.id

    def test_fuzzy_fallback_still_used_when_no_exact_match_exists(self, tmp_path: Path):
        """When nothing matches exactly, the substring fallback should still
        resolve (preserves existing fuzzy-matching behavior as a last resort)."""
        derived_output = tmp_path / "runs" / "run1" / "sample_summary.stats.txt"
        derived_output.parent.mkdir(parents=True)
        derived_output.write_text("fake-stats")

        session = LedgerSession()
        session.entries.append(
            LedgerEntry(
                kind=EntryKind.TOOL_RUN,
                tool_spec_name="samtools-flagstat",
                files=[
                    FileRef(path=str(derived_output), sha256="x", size_bytes=1, role="output"),
                ],
            )
        )

        agent = _make_agent(dataset=None, home_dir=tmp_path / "home", session=session)
        tool_request = _make_tool_request("some-tool", {"stats": "sample_summary"})

        input_files, parent_id = _resolve_inputs(agent, tool_request)

        assert input_files["stats"] == derived_output

    def test_raises_when_nothing_resolves(self, tmp_path: Path):
        session = LedgerSession()
        agent = _make_agent(dataset=None, home_dir=tmp_path / "home", session=session)
        tool_request = _make_tool_request("some-tool", {"ref_fasta": "does_not_exist.fna"})

        with pytest.raises(ValueError):
            _resolve_inputs(agent, tool_request)
