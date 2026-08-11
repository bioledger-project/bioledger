from __future__ import annotations

import re

from bioledger.toolspec.models import ExecutionSpec, ParamType


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


def validate_nextflow_dsl2(nf_str: str) -> list[str]:
    """Structural checks on generated Nextflow DSL2 process."""
    issues: list[str] = []
    if not re.search(r"process\s+\w+\s*\{", nf_str):
        issues.append("No process block found")
    if "container" not in nf_str:
        issues.append("Missing container directive")
    if "input:" not in nf_str:
        issues.append("Missing input: block")
    if "output:" not in nf_str:
        issues.append("Missing output: block")
    if not re.search(r"(script|shell):\s*\n", nf_str):
        issues.append("Missing script: or shell: block")
    if nf_str.count("{") != nf_str.count("}"):
        issues.append("Unbalanced braces in process definition")
    return issues
