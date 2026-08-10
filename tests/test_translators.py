from __future__ import annotations

from bioledger.forges.toolforge.translators._export_validate import (
    validate_galaxy_xml,
    validate_nextflow_dsl2,
)
from bioledger.forges.toolforge.translators.galaxy import to_galaxy_xml
from bioledger.forges.toolforge.translators.nextflow import (
    from_nextflow_module,
    to_nextflow_process,
)


def test_galaxy_xml_roundtrip(sample_exec_spec):
    xml_str = to_galaxy_xml(sample_exec_spec)
    assert '<?xml' in xml_str
    assert 'id="fastqc"' in xml_str
    assert '<command' in xml_str


def test_nextflow_export(sample_exec_spec):
    nf_str = to_nextflow_process(sample_exec_spec)
    assert "process fastqc" in nf_str
    assert "container" in nf_str
    assert "input:" in nf_str
    assert "output:" in nf_str
    assert "script:" in nf_str


def test_nextflow_import(sample_nextflow_dsl2):
    spec = from_nextflow_module(sample_nextflow_dsl2)
    assert spec.name == "fastqc"
    assert "quay.io/biocontainers/fastqc" in spec.container
    assert spec.get_input("reads") is not None
    assert spec.get_output("report") is not None


def test_validate_galaxy_xml_valid(sample_exec_spec):
    xml_str = to_galaxy_xml(sample_exec_spec)
    issues = validate_galaxy_xml(xml_str)
    assert len(issues) == 0


def test_validate_galaxy_xml_invalid():
    issues = validate_galaxy_xml("<not_a_tool/>")
    assert len(issues) > 0
    assert any("Root element" in i for i in issues)


def test_validate_galaxy_xml_parse_error():
    issues = validate_galaxy_xml("<<<not xml>>>")
    assert len(issues) == 1
    assert "parse error" in issues[0].lower()


def test_validate_nextflow_valid(sample_exec_spec):
    nf_str = to_nextflow_process(sample_exec_spec)
    issues = validate_nextflow_dsl2(nf_str)
    assert len(issues) == 0


def test_validate_nextflow_invalid():
    issues = validate_nextflow_dsl2("// just a comment")
    assert len(issues) > 0
    assert any("process" in i.lower() for i in issues)


def test_nextflow_roundtrip(sample_exec_spec):
    """Export to NF, import back, check key fields survive."""
    nf_str = to_nextflow_process(sample_exec_spec)
    parsed = from_nextflow_module(nf_str)
    assert parsed.name == "fastqc"
    assert parsed.container == sample_exec_spec.container
    assert parsed.get_input("reads") is not None


