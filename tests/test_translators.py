from __future__ import annotations

from bioledger.forges.toolforge.translators._export_validate import (
    validate_galaxy_xml,
    validate_nextflow_dsl2,
)
from bioledger.forges.toolforge.translators.galaxy import from_galaxy_xml, to_galaxy_xml
from bioledger.forges.toolforge.translators.nextflow import (
    from_nextflow_module,
    to_nextflow_process,
)
from bioledger.toolspec.models import ParamType


def test_galaxy_xml_roundtrip(sample_exec_spec):
    xml_str = to_galaxy_xml(sample_exec_spec)
    assert '<?xml' in xml_str
    assert 'id="fastqc"' in xml_str
    assert '<command' in xml_str

    # Parse back
    parsed = from_galaxy_xml(xml_str)
    assert parsed.name == "fastqc"
    assert parsed.container == "quay.io/biocontainers/fastqc:0.11.9--0"
    assert parsed.get_input("reads") is not None
    assert parsed.get_output("report") is not None
    assert parsed.get_parameter("threads") is not None


def test_galaxy_xml_import(sample_galaxy_xml):
    spec = from_galaxy_xml(sample_galaxy_xml)
    assert spec.name == "fastqc"
    assert spec.version == "0.11.9"
    reads_input = spec.get_input("reads")
    assert reads_input is not None
    assert reads_input.type == ParamType.FILE
    assert reads_input.format == "fastq"
    threads_param = spec.get_parameter("threads")
    assert threads_param is not None
    assert threads_param.type == ParamType.INTEGER
    assert spec.get_output("report") is not None


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


def test_galaxy_xml_empty_integer_value():
    """Regression test: Galaxy XML with value="" should not crash on integer params.

    Some Galaxy tools have empty value attributes (value="") for optional
    integer parameters. This should be treated as unset/default (0), not
    cause a ValueError during import.
    """
    xml_with_empty_value = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command>echo test</command>
    <inputs>
        <param name="optional_int" type="integer" value="" label="Optional int"/>
        <param name="set_int" type="integer" value="5" min="1" max="10" label="Set int"/>
    </inputs>
    <outputs>
        <data name="out" format="txt"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_empty_value)
    assert spec.name == "test_tool"
    optional_int = spec.get_parameter("optional_int")
    assert optional_int is not None
    assert optional_int.type == ParamType.INTEGER
    assert optional_int.default == 0  # Empty string → 0 default
    set_int = spec.get_parameter("set_int")
    assert set_int is not None
    assert set_int.default == 5
    assert set_int.min == 1
    assert set_int.max == 10


def test_galaxy_xml_argument_fallback():
    """Regression test: Galaxy XML params with 'argument' attr use that as name.

    Galaxy tools often use argument="--adapters" instead of name="adapters".
    The parser should strip leading dashes and convert to valid identifier.
    """
    xml_with_argument = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command>echo test</command>
    <inputs>
        <param argument="--adapters" type="data" format="tabular" label="Adapter list"/>
        <param argument="--min-length" type="integer" value="10" label="Min length"/>
        <param name="explicit_name" type="data" format="fastq" label="Explicit"/>
    </inputs>
    <outputs>
        <data name="out" format="txt"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_argument)
    assert spec.get_input("adapters") is not None  # --adapters → adapters
    assert spec.get_parameter("min_length") is not None  # --min-length → min_length
    assert spec.get_input("explicit_name") is not None  # explicit name preserved
    # Check no empty keys
    assert all(name != "" for name in spec.inputs)
    assert all(name != "" for name in spec.parameters)


def test_galaxy_xml_package_to_container():
    """Regression test: Galaxy package requirements convert to biocontainers.

    Galaxy XML uses <requirement type="package" version="x">name</requirement>
    instead of <container>. Should convert to quay.io/biocontainers/name:x--build_string
    with proper conda-style build string suffix.
    """
    xml_with_package = """<?xml version="1.0"?>
<tool id="fastqc" name="FastQC" version="0.74">
    <requirements>
        <requirement type="package" version="0.12.1">fastqc</requirement>
    </requirements>
    <command>fastqc --help</command>
    <inputs>
        <param name="input" type="data" format="fastq"/>
    </inputs>
    <outputs>
        <data name="out" format="html"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_package)
    # Biocontainers uses conda-style tags: version--build_string (e.g., 0.12.1--hdfd78af_0)
    assert spec.container == "quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0"
    assert spec.get_input("input") is not None


def test_galaxy_xml_optional_inputs():
    """Regression test: Galaxy optional="true" sets required=False on inputs."""
    xml_with_optional = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command>echo test</command>
    <inputs>
        <param name="required_input" type="data" format="fastq"/>
        <param name="optional_input" type="data" format="tabular" optional="true"/>
        <param name="also_optional" type="data" format="txt" optional="True"/>
    </inputs>
    <outputs>
        <data name="out" format="txt"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_optional)
    assert spec.get_input("required_input").required is True
    assert spec.get_input("optional_input").required is False
    assert spec.get_input("also_optional").required is False


def test_galaxy_xml_multi_format():
    """Regression test: Galaxy format="a,b,c" takes first format.

    Galaxy allows multiple formats like "fastq,fastq.gz,bam".
    We take the first one as the primary format.
    """
    xml_multi_format = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command>echo test</command>
    <inputs>
        <param name="multi" type="data" format="fastq,fastq.gz,bam,sam"/>
    </inputs>
    <outputs>
        <data name="out" format="txt"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_multi_format)
    assert spec.get_input("multi").format == "fastq"  # First format only


def test_galaxy_xml_cheetah_to_jinja2():
    """Regression test: Galaxy Cheetah template converted to Jinja2.

    Galaxy uses Cheetah templating (#set, #if, $var) while BioLedger uses Jinja2.
    The importer should convert syntax and handle Galaxy-specific attributes.
    """
    xml_with_cheetah = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command><![CDATA[
        #import re
        #set base = $input_file.element_identifier
        #if $input_file.ext.endswith('.gz'):
            gunzip -c $input_file > temp.txt
        #else:
            cp $input_file temp.txt
        #end if
        process --in ${input_file} --out ${html_file.files_path}/result.txt
    ]]></command>
    <inputs>
        <param name="input_file" type="data" format="fastq"/>
    </inputs>
    <outputs>
        <data name="html_file" format="html"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_cheetah)
    cmd = spec.command

    # Python imports removed
    assert "#import" not in cmd

    # #set converted to {% set %}
    assert "{% set" in cmd
    assert "#set" not in cmd

    # #if converted to {% if %}
    assert "{% if" in cmd
    assert "{% endif %}" in cmd
    assert "#if" not in cmd
    assert "#end if" not in cmd

    # $var converted to {{inputs.var}} or {{parameters.var}}
    assert "{{inputs.input_file}}" in cmd or "{{ inputs.input_file }}" in cmd
    assert "{{outputs._dir}}" in cmd or "{{ outputs._dir }}" in cmd

    # Galaxy-specific .element_identifier converted to basename filter
    assert "| basename" in cmd

    # Galaxy-specific .files_path converted to outputs._dir
    assert "outputs._dir" in cmd


def test_galaxy_xml_env_var_pattern():
    """Regression test: Galaxy ${VAR:-default} pattern converted to parameter.

    Galaxy uses ${GALAXY_SLOTS:-2} for threads. Should extract default value
    and convert to BioLedger parameter reference.
    """
    xml_with_env_var = """<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="1.0">
    <requirements>
        <container type="docker">test:latest</container>
    </requirements>
    <command><![CDATA[
        fastqc --threads ${GALAXY_SLOTS:-4} --outdir output $input_file
    ]]></command>
    <inputs>
        <param name="input_file" type="data" format="fastq"/>
        <param argument="--threads" type="integer" value="${GALAXY_SLOTS:-4}" min="1" max="16"/>
        <param argument="--outdir" type="text" value=""/>
    </inputs>
    <outputs>
        <data name="out" format="txt"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_env_var)

    # Should have threads parameter with default 4 extracted from ${GALAXY_SLOTS:-4}
    threads_param = spec.get_parameter("threads")
    assert threads_param is not None
    assert threads_param.type == ParamType.INTEGER
    assert threads_param.default == 4
    assert threads_param.min == 1
    assert threads_param.max == 16

    # Command should reference parameters.threads
    assert "parameters.threads" in spec.command or "{{threads}}" in spec.command


def test_galaxy_xml_macro_detection():
    """Test that Galaxy macros like @TOOL_VERSION@ are detected and handled.

    When macros can't be resolved, the importer should:
    - Detect the macro pattern
    - Leave version empty (no guessing from biocontainers)
    - Warn the user to set the version manually
    - Continue with package-based container lookup
    """
    xml_with_macros = """<?xml version="1.0"?>
<tool id="fastp" name="fastp" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@">
    <requirements>
        <requirement type="package" version="@TOOL_VERSION@">fastp</requirement>
    </requirements>
    <command><![CDATA[
        fastp -i $input
    ]]></command>
    <inputs>
        <param name="input" type="data" format="fastq"/>
    </inputs>
    <outputs>
        <data name="out" format="fastq"/>
    </outputs>
</tool>
"""
    spec = from_galaxy_xml(xml_with_macros)

    # Version should be empty when macros can't be resolved — no guessing
    assert spec.version == ""

    # Container should still be resolved with specific tag (not 'latest' or macro)
    assert "@TOOL_VERSION@" not in spec.container
    assert ":latest" not in spec.container
    assert spec.container.startswith("quay.io/biocontainers/fastp:")
    # Should have a specific build string (e.g., "1.3.3--h43da1c4_0")
    assert "--" in spec.container
