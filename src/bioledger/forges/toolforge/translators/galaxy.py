from __future__ import annotations

import xml.etree.ElementTree as ET

from bioledger.core.llm.agents import ForgeDeps
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    ToolSpec,
)


def to_galaxy_xml(spec: ExecutionSpec) -> str:
    """Convert an ExecutionSpec to Galaxy tool XML. Uses ElementTree for proper escaping."""
    tool = ET.Element("tool", id=spec.name, name=spec.name, version=spec.version or "0.1")
    ET.SubElement(tool, "description").text = spec.description

    reqs = ET.SubElement(tool, "requirements")
    ET.SubElement(reqs, "container", type="docker").text = spec.container

    cmd = ET.SubElement(tool, "command", detect_errors="exit_code")
    cmd.text = spec.command  # ElementTree handles escaping

    inputs_el = ET.SubElement(tool, "inputs")
    for name, inp in spec.inputs.items():
        ET.SubElement(
            inputs_el, "param", name=name, type="data",
            format=inp.format, label=inp.description or name,
        )

    for name, param in spec.parameters.items():
        attrs: dict[str, str] = {"name": name, "label": param.description or name}
        if param.type == ParamType.SELECT and param.options:
            attrs["type"] = "select"
            sel = ET.SubElement(inputs_el, "param", **attrs)
            for opt in param.options:
                ET.SubElement(sel, "option", value=opt).text = opt
        elif param.type == ParamType.INTEGER:
            attrs.update(type="integer", value=str(param.default or ""))
            ET.SubElement(inputs_el, "param", **attrs)
        elif param.type == ParamType.FLOAT:
            attrs.update(type="float", value=str(param.default or ""))
            ET.SubElement(inputs_el, "param", **attrs)
        elif param.type == ParamType.BOOLEAN:
            attrs.update(type="boolean", checked=str(param.default or False).lower())
            ET.SubElement(inputs_el, "param", **attrs)
        else:
            attrs.update(type="text", value=str(param.default or ""))
            ET.SubElement(inputs_el, "param", **attrs)

    outputs_el = ET.SubElement(tool, "outputs")
    for name, out in spec.outputs.items():
        ET.SubElement(
            outputs_el, "data", name=name, format=out.format,
            label=out.description or name,
        )

    ET.indent(tool)
    return ET.tostring(tool, encoding="unicode", xml_declaration=True)


async def export_galaxy_tool(
    spec: ToolSpec,
    deps: ForgeDeps,
    agent: "ToolForgeAgent",  # noqa: F821
    use_llm: bool = True,
) -> str:
    """Export: BioLedger Spec -> Galaxy XML.
    Steps: generate -> validate -> LLM fix (if needed) -> LLM enrich -> re-validate."""
    from ._export_validate import validate_galaxy_xml

    # Step 1: Programmatic generation
    xml_str = to_galaxy_xml(spec.execution)

    # Step 2: Structural validation
    issues = validate_galaxy_xml(xml_str)

    # Step 3: LLM fix if structural issues
    if issues and use_llm:
        issues_str = "\n".join(f"- {i}" for i in issues)
        xml_str = await agent.enrich_export(
            spec, f"ISSUES:\n{issues_str}\n\nXML:\n{xml_str}", "Galaxy XML", deps
        )
        issues = validate_galaxy_xml(xml_str)

    # Step 4: LLM enrichment (add help, citations, test cases)
    if use_llm and not issues:
        xml_str = await agent.enrich_export(spec, xml_str, "Galaxy XML", deps)

    return xml_str
