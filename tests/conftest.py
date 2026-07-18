from __future__ import annotations

import pytest

from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolParameter,
    ToolSpec,
)


@pytest.fixture
def sample_exec_spec() -> ExecutionSpec:
    """Minimal valid ExecutionSpec for testing."""
    return ExecutionSpec(
        name="fastqc",
        version="0.11.9",
        description="Quality control for sequencing data",
        container="quay.io/biocontainers/fastqc:0.11.9--0",
        command="fastqc {{inputs.reads}} -o {{outputs._dir}} -t {{parameters.threads}}",
        inputs={
            "reads": ToolInput(
                type=ParamType.FILE,
                format="fastq",
                description="Input reads",
            ),
        },
        outputs={
            "report": ToolOutput(
                format="html",
                description="QC report",
                pattern="*_fastqc.html",
            ),
        },
        parameters={
            "threads": ToolParameter(
                type=ParamType.INTEGER,
                default=4,
                min=1,
                max=32,
                description="Number of threads",
            ),
        },
        categories=["quality-control"],
        status=SpecStatus.VALID,
    )


@pytest.fixture
def sample_tool_spec(sample_exec_spec: ExecutionSpec) -> ToolSpec:
    """ToolSpec wrapping the sample ExecutionSpec."""
    return ToolSpec(execution=sample_exec_spec)


@pytest.fixture
def sample_galaxy_xml() -> str:
    """Minimal Galaxy tool XML for import testing."""
    return """\
<?xml version='1.0' encoding='us-ascii'?>
<tool id="fastqc" name="FastQC" version="0.11.9">
  <description>Read Quality reports</description>
  <requirements>
    <container type="docker">quay.io/biocontainers/fastqc:0.11.9--0</container>
  </requirements>
  <command detect_errors="exit_code">fastqc $reads -o $report -t $threads</command>
  <inputs>
    <param name="reads" type="data" format="fastq" label="Input reads" />
    <param name="threads" type="integer" value="4" min="1" max="32" label="Threads" />
  </inputs>
  <outputs>
    <data name="report" format="html" label="QC Report" />
  </outputs>
</tool>
"""


@pytest.fixture
def sample_nextflow_dsl2() -> str:
    """Minimal Nextflow DSL2 process for import testing."""
    return """\
process fastqc {
    container 'quay.io/biocontainers/fastqc:0.11.9--0'

    input:
    path reads

    output:
    path "*_fastqc.html", emit: report

    script:
    \"\"\"
    fastqc ${reads} -o . -t 4
    \"\"\"
}
"""


@pytest.fixture
def tmp_tools_dir(tmp_path):
    """Temporary directory for ToolStore tests."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    return tools_dir


@pytest.fixture
def tmp_db_path(tmp_path):
    """Temporary path for LedgerStore SQLite database."""
    return tmp_path / "test_ledger.db"


@pytest.fixture
def docker_available() -> bool:
    """Check whether a Docker daemon is reachable. Tests that require
    Docker should use this and pytest.skip() if False."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False
