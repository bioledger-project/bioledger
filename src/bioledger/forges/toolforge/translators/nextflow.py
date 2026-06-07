from __future__ import annotations

import re
from pathlib import Path

from bioledger.core.llm.agents import ForgeDeps
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolSpec,
)
from bioledger.toolspec.validate import validate_execution


def to_nextflow_process(spec: ExecutionSpec) -> str:
    """Convert an ExecutionSpec to a Nextflow DSL2 process.
    Handles inputs, outputs, parameters (as val inputs + params block), and publishDir."""
    input_decls = []
    for name in spec.inputs:
        input_decls.append(f"    path {name}")
    for name in spec.parameters:
        input_decls.append(f"    val {name}")

    output_decls = []
    for name, out in spec.outputs.items():
        pattern = out.pattern or f"*.{out.format}"
        output_decls.append(f'    path "{pattern}", emit: {name}')

    # Build script with parameter substitution
    script = spec.command
    for name in spec.parameters:
        # Replace Jinja2 template vars with NF variable refs
        script = script.replace(f"{{{{params.{name}}}}}", f"${{{name}}}")
        script = script.replace(f"{{{{parameters.{name}}}}}", f"${{{name}}}")

    # Build params defaults block (goes outside the process, but useful as a reference)
    params_lines = []
    for name, param in spec.parameters.items():
        default = param.default
        if default is None:
            if param.type == ParamType.INTEGER:
                default = 0
            elif param.type == ParamType.BOOLEAN:
                default = False
            else:
                default = ""
        if isinstance(default, str):
            params_lines.append(f"params.{name} = '{default}'")
        elif isinstance(default, bool):
            params_lines.append(f"params.{name} = {str(default).lower()}")
        else:
            params_lines.append(f"params.{name} = {default}")

    params_block = ""
    if params_lines:
        params_block = "// Default parameters\n" + "\n".join(params_lines) + "\n\n"

    return f"""\
{params_block}process {spec.name} {{
    container '{spec.container}'

    input:
{chr(10).join(input_decls)}

    output:
{chr(10).join(output_decls)}

    script:
    \"\"\"
    {script}
    \"\"\"
}}"""


def from_nextflow_module(dsl2_str: str) -> ExecutionSpec:
    """Parse a Nextflow DSL2 process block into a BioLedger ExecutionSpec.
    Handles standard process { container, input, output, script } blocks."""

    # Extract process name
    name_match = re.search(r"process\s+(\w+)\s*\{", dsl2_str)
    name = name_match.group(1) if name_match else "unknown"

    # Extract container
    container_match = re.search(r"container\s+['\"]([^'\"]+)['\"]", dsl2_str)
    container = container_match.group(1) if container_match else ""

    # Extract input declarations: "path <name>" or "tuple val(x), path(y)"
    inputs: dict[str, ToolInput] = {}
    input_block = re.search(
        r"input:\s*\n(.*?)(?=\n\s*(output:|script:|shell:|exec:))", dsl2_str, re.DOTALL
    )
    if input_block:
        for line in input_block.group(1).strip().splitlines():
            line = line.strip()
            path_match = re.match(r"path\s+(\w+)", line)
            if path_match:
                iname = path_match.group(1)
                inputs[iname] = ToolInput(
                    name=iname, type=ParamType.FILE, format="any"
                )

    # Extract output declarations
    outputs: dict[str, ToolOutput] = {}
    output_block = re.search(
        r"output:\s*\n(.*?)(?=\n\s*(script:|shell:|exec:|process\s|\Z))",
        dsl2_str,
        re.DOTALL,
    )
    if output_block:
        for line in output_block.group(1).strip().splitlines():
            line = line.strip()
            path_match = re.match(
                r'path\s+["\']([^"\']+)["\'](?:,\s*emit:\s*(\w+))?', line
            )
            if path_match:
                pattern = path_match.group(1)
                emit_name = path_match.group(2) or f"out_{len(outputs)}"
                outputs[emit_name] = ToolOutput(
                    name=emit_name, pattern=pattern, format="any"
                )

    # Extract script block
    script_match = re.search(
        r'(?:script|shell):\s*\n\s*"""\s*\n(.*?)\n\s*"""', dsl2_str, re.DOTALL
    )
    command = script_match.group(1).strip() if script_match else ""

    return ExecutionSpec(
        name=name,
        container=container,
        command=command,
        inputs=inputs,
        outputs=outputs,
        status=SpecStatus.DRAFT,
    )


async def import_nextflow_module(
    nf_path: Path,
    deps: ForgeDeps,
    agent: "ToolForgeAgent",  # noqa: F821
    use_llm: bool = True,
) -> ToolSpec:
    """Import a Nextflow DSL2 process into a BioLedger ToolSpec."""
    dsl2_str = nf_path.read_text()

    # Step 1: Programmatic translation
    try:
        exec_spec = from_nextflow_module(dsl2_str)
    except Exception as e:
        if not use_llm:
            raise
        exec_spec = await agent.parse(dsl2_str, "Nextflow DSL2", str(e), deps)

    # Step 2: Programmatic validation
    result = validate_execution(exec_spec, strict=False)

    # Step 3: LLM fix
    if not result.is_valid and use_llm:
        exec_spec = await agent.fix(exec_spec, result.issues, deps)
        validate_execution(exec_spec, strict=False)

    # Step 4: LLM conceptual validation
    if use_llm:
        conceptual_issues = await agent.review(exec_spec, "Nextflow DSL2", deps)
        for issue in conceptual_issues:
            print(f"  {issue}")

    return ToolSpec(execution=exec_spec)


async def export_nextflow_module(
    spec: ToolSpec,
    deps: ForgeDeps,
    agent: "ToolForgeAgent",  # noqa: F821
    use_llm: bool = True,
) -> str:
    """Export: BioLedger Spec -> Nextflow DSL2 process.
    Steps: generate -> validate -> LLM fix (if needed) -> LLM enrich -> re-validate."""
    from ._export_validate import validate_nextflow_dsl2

    # Step 1: Programmatic generation
    nf_str = to_nextflow_process(spec.execution)

    # Step 2: Structural validation
    issues = validate_nextflow_dsl2(nf_str)

    # Step 3: LLM fix if structural issues
    if issues and use_llm:
        issues_str = "\n".join(f"- {i}" for i in issues)
        nf_str = await agent.enrich_export(
            spec, f"ISSUES:\n{issues_str}\n\nNextflow:\n{nf_str}", "Nextflow DSL2", deps
        )
        issues = validate_nextflow_dsl2(nf_str)

    # Step 4: LLM enrichment
    if use_llm and not issues:
        nf_str = await agent.enrich_export(spec, nf_str, "Nextflow DSL2", deps)

    return nf_str
