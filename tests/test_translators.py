from __future__ import annotations

from bioledger.forges.toolforge.translators.nextflow import (
    to_nextflow_process,
    validate_nextflow_dsl2,
)


def test_nextflow_export(sample_exec_spec):
    nf_str = to_nextflow_process(sample_exec_spec)
    assert "process fastqc" in nf_str
    assert "container" in nf_str
    assert "input:" in nf_str
    assert "output:" in nf_str
    assert "script:" in nf_str


def test_validate_nextflow_valid(sample_exec_spec):
    nf_str = to_nextflow_process(sample_exec_spec)
    issues = validate_nextflow_dsl2(nf_str)
    assert len(issues) == 0


def test_validate_nextflow_invalid():
    issues = validate_nextflow_dsl2("// just a comment")
    assert len(issues) > 0
    assert any("process" in i.lower() for i in issues)
