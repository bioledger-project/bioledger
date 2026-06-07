from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from bioledger.core.llm.agents import ForgeDeps
from bioledger.toolspec.containers import (
    has_macro,
    lookup_biocontainers_tag_sync,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolOutput,
    ToolParameter,
    ToolSpec,
)
from bioledger.toolspec.validate import validate_execution

from .galaxy_context import GalaxyImportContext, resolve_import_context
from .galaxy_macros import expand_text_macros, expand_xml_macros, load_macros

console = Console()


def _resolve_version_for_pkg(version_str: str, pkg_name: str = "") -> str:
    """Resolve a Galaxy ``version=`` attribute that may contain unresolved macros.

    If the version is a usable literal, return it. Otherwise return empty
    string — we do NOT guess versions from biocontainers. An empty version
    will surface as a validation issue so the user must set it manually.
    """
    if version_str and not has_macro(version_str):
        return version_str.strip()
    return ""


def _requirement_version(req) -> str:
    """Extract version from a ``<requirement>`` element, treating macros as empty."""
    version = req.get("version", "").strip()
    if has_macro(version):
        return ""
    return version


def _convert_cheetah_to_jinja2(
    cheetah_cmd: str, input_names: set[str], param_names: set[str],
    env_var_map: dict[str, str] | None = None
) -> str:
    """Convert a Galaxy Cheetah command template to BioLedger Jinja2.

    Programmatic conversion handles:
        * ``#set`` → ``{% set %}``
        * ``#if`` / ``#elif`` / ``#else`` / ``#end if`` → Jinja conditionals
        * ``$var`` / ``${var}`` → ``{{inputs.var}}`` or ``{{parameters.var}}``
        * ``.element_identifier`` → ``| basename`` filter
        * ``.ext`` → ``| basename | splitext | last`` filter

    Galaxy-specific constructs that don't have a clean Jinja equivalent
    (e.g. ``.files_path``, ``.dataset``) are passed through verbatim and
    typically require an LLM ``fix_galaxy_import`` pass to repair.

    Args:
        cheetah_cmd: The raw ``<command>`` block content from a Galaxy XML.
        input_names: Names of declared inputs (used to namespace bare ``$x``).
        param_names: Names of declared parameters (same).
        env_var_map: Mapping of Galaxy env vars (e.g. ``GALAXY_SLOTS``) to
            BioLedger parameter names (e.g. ``threads``).
    """
    lines = cheetah_cmd.split('\n')
    result_lines = []

    for line in lines:
        stripped = line.strip()

        # Skip Python imports (Galaxy-specific, not needed in BioLedger)
        if stripped.startswith('#import'):
            continue

        # Convert #set statements: #set var = value → {% set var = value %}
        if stripped.startswith('#set '):
            content = stripped[5:]  # Remove '#set '
            # Convert $var to {{var}} in the expression
            content = re.sub(r'\$(\w+)', r'{{\1}}', content)
            content = re.sub(r'\$\{(\w+)\}', r'{{\1}}', content)
            line = line.replace(stripped, f'{{% set {content} %}}')

        # Convert conditionals
        elif stripped.startswith('#if '):
            condition = stripped[4:]
            # Convert variable references in condition
            condition = _convert_cheetah_vars(condition, input_names, param_names, env_var_map)
            line = line.replace(stripped, f'{{% if {condition} %}}')
        elif stripped.startswith('#elif '):
            condition = stripped[6:]
            condition = _convert_cheetah_vars(condition, input_names, param_names, env_var_map)
            line = line.replace(stripped, f'{{% elif {condition} %}}')
        elif stripped == '#else:':
            line = line.replace(stripped, '{% else %}')
        elif stripped in ('#end if', '#endif'):
            line = line.replace(stripped, '{% endif %}')

        # Convert variable references in regular lines
        elif not stripped.startswith('#'):
            line = _convert_cheetah_vars(line, input_names, param_names, env_var_map)

        result_lines.append(line)

    return '\n'.join(result_lines)


def _convert_cheetah_vars(
    text: str,
    input_names: set[str],
    param_names: set[str],
    env_var_map: dict[str, str] | None = None
) -> str:
    """Convert Cheetah ``$var`` references to Jinja2 ``{{namespace.var}}``."""
    env_var_map = env_var_map or {}

    def replace_var(match):
        # Match $var or ${var} (with optional backslash escape) - capture just the variable name
        full_match = match.group(0)  # e.g., $input_file or ${input_file} or \${input_file}

        # Strip leading backslash if present (from Galaxy escape sequences)
        if full_match.startswith('\\'):
            full_match = full_match[1:]

        # Remove $ and optional braces to get the variable name
        if full_match.startswith('${'):
            inner = full_match[2:-1]  # Remove ${ and }
        else:
            inner = full_match[1:]  # Remove $

        # Check if this is a Galaxy env var (e.g., ${GALAXY_SLOTS:-4})
        # Extract just the var name if there's a :-default pattern
        var_name = inner.split(':-')[0].split(':')[0]

        # If it's a mapped env var, use the BioLedger parameter name
        if var_name in env_var_map:
            bioledger_name = env_var_map[var_name]
            return f"{{{{parameters.{bioledger_name}}}}}"

        # Split on dot to handle attributes like $input_file.ext
        parts = inner.split('.')
        base_var = parts[0].split(':-')[0].split(':')[0]  # Handle ${VAR:-default}
        attrs = parts[1:]

        # Determine namespace
        if base_var in input_names:
            namespace = "inputs"
        elif base_var in param_names:
            namespace = "parameters"
        else:
            # Unknown variable - could be a local set variable, pass through
            namespace = None

        # Build Jinja2 reference
        if namespace:
            base = f"{{{{{namespace}.{base_var}}}}}"
        else:
            base = f"{{{{{base_var}}}}}"

        # Handle common Galaxy attributes
        for attr in attrs:
            if attr == 'ext':
                # File extension - extract from path with Jinja2 filter
                if namespace:
                    base = "{{" + f"{namespace}.{base_var} | basename | splitext | last" + "}}"
            elif attr == 'element_identifier':
                # Original filename - use basename filter
                if namespace:
                    tmpl = "{{ {ns}.{var} | basename }}"
                    base = tmpl.format(ns=namespace, var=base_var)
            elif attr == 'files_path':
                # Output directory - BioLedger uses outputs._dir
                base = "{{outputs._dir}}"
            else:
                # Other attributes - pass through as-is (may need manual fix)
                base = base + f".{attr}"

        return base

    # Replace ${var} first (more specific), then $var
    # Also handle escaped \${var} patterns from Galaxy XML (CDATA escapes)
    # Galaxy XML often has \${VAR} in CDATA which becomes \${VAR} in text
    # Use re.escape to properly match the backslash in regex
    # Include - in char class for ${VAR:-default} syntax
    escaped_bs_dollar_brace = re.escape("\\") + re.escape("$") + re.escape("{")
    text = re.sub(escaped_bs_dollar_brace + r"[\w.:-]+\}", replace_var, text)
    text = re.sub(r"\$\{[\w.:-]+\}", replace_var, text)  # ${var} pattern
    text = re.sub(r"\$[\w.]+", replace_var, text)  # Simple $var

    return text


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
            attrs.update(type="integer", value=str(param.default or 0))
            if param.min is not None:
                attrs["min"] = str(param.min)
            if param.max is not None:
                attrs["max"] = str(param.max)
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


@dataclass
class GalaxyParseResult:
    """Output of :func:`parse_galaxy_xml`.

    ``warnings`` collects unresolved Galaxy macros, biocontainers lookup
    failures, and other facts the orchestrator may want to forward to the
    user, the LLM fix step, or both.
    """

    spec: ExecutionSpec
    warnings: list[str]


def parse_galaxy_xml(xml_str: str) -> GalaxyParseResult:
    """Pure parser: Galaxy tool XML → (ExecutionSpec, warnings).

    Performs no console output; callers decide how to surface warnings.
    """
    root = ET.fromstring(xml_str)

    inputs: dict[str, ToolInput] = {}
    parameters: dict[str, ToolParameter] = {}
    outputs: dict[str, ToolOutput] = {}

    for param in root.findall(".//inputs/param"):
        # Galaxy uses 'name' or 'argument' (e.g., argument="--adapters")
        name = param.get("name", "")
        if not name:
            # Fall back to argument, stripping leading dashes
            arg = param.get("argument", "")
            name = arg.lstrip("-").replace("-", "_") if arg else ""
        ptype = param.get("type", "text")
        # Check optional flag (Galaxy uses optional="true")
        is_optional = param.get("optional", "false").lower() == "true"

        if ptype == "data":
            fmt = param.get("format", "any")
            # Take first format if multiple (e.g., "fastq,fastq.gz,bam")
            if "," in fmt:
                fmt = fmt.split(",")[0]
            inputs[name] = ToolInput(
                name=name,
                type=ParamType.FILE,
                format=fmt,
                required=not is_optional,
                description=param.get("label", ""),
            )
        elif ptype == "integer":
            # Handle empty string values (value="" should use default, not crash)
            raw_value = param.get("value", "")
            default = 0

            # Handle Galaxy env var patterns like ${GALAXY_SLOTS:-2} or ${VAR:default}
            if raw_value.startswith("${") and raw_value.endswith("}"):
                # Extract default from ${VAR:-default} or ${VAR:default}
                inner = raw_value[2:-1]  # Remove ${ and }
                if ":-" in inner:
                    _, default_str = inner.rsplit(":-", 1)
                    try:
                        default = int(default_str)
                    except ValueError:
                        default = 0
                elif ":" in inner:
                    _, default_str = inner.rsplit(":", 1)
                    try:
                        default = int(default_str)
                    except ValueError:
                        default = 0
            else:
                try:
                    default = int(raw_value) if raw_value else 0
                except ValueError:
                    default = 0

            raw_min = param.get("min")
            raw_max = param.get("max")
            parameters[name] = ToolParameter(
                name=name,
                type=ParamType.INTEGER,
                default=default,
                min=int(raw_min) if raw_min else None,
                max=int(raw_max) if raw_max else None,
                description=param.get("label", ""),
            )
        elif ptype == "select":
            opts = [opt.get("value", "") for opt in param.findall("option")]
            parameters[name] = ToolParameter(
                name=name,
                type=ParamType.SELECT,
                options=opts,
                description=param.get("label", ""),
            )
        elif ptype == "boolean":
            parameters[name] = ToolParameter(
                name=name,
                type=ParamType.BOOLEAN,
                default=param.get("checked", "false").lower() == "true",
                description=param.get("label", ""),
            )

    for data in root.findall(".//outputs/data"):
        name = data.get("name", "")
        outputs[name] = ToolOutput(
            name=name,
            format=data.get("format", "any"),
            description=data.get("label", ""),
        )

    # Container resolution: prefer biocontainers (reproducible, audit-friendly)
    # over arbitrary <container> tags whenever a <requirement type="package">
    # is declared. We only fall back to the explicit <container> tag when no
    # package requirement is present or biocontainers lookup fails.
    container = ""
    pkg_name = ""  # Track for version lookup
    unresolved_macros: list[str] = []

    explicit_container = ""
    for req in root.findall(".//requirements/container"):
        if req.get("type") == "docker":
            explicit_container = (req.text or "").strip()
            if has_macro(explicit_container):
                unresolved_macros.append(f"container: {explicit_container}")
                explicit_container = ""
            break

    for req in root.findall(".//requirements/requirement"):
        if req.get("type") == "package":
            pkg_name = (req.text or "").strip()
            pkg_version = _requirement_version(req)

            if pkg_name:
                if has_macro(pkg_name):
                    unresolved_macros.append(f"package name: {pkg_name}")
                    pkg_name = pkg_name.replace("@", "").lower()

                container = lookup_biocontainers_tag_sync(pkg_name, pkg_version) or ""
                break

    if not container and explicit_container:
        container = explicit_container
    elif container and explicit_container and container != explicit_container:
        unresolved_macros.append(
            f"explicit container '{explicit_container}' overridden by "
            f"biocontainers resolution '{container}' for reproducibility"
        )

    if not container and not explicit_container and pkg_name:
        # We had a package requirement but biocontainers lookup failed —
        # surface this rather than silently fabricating an image URL.
        unresolved_macros.append(
            f"biocontainers lookup failed for package '{pkg_name}'; "
            "container must be set manually"
        )

    # Build mapping from Galaxy env vars to BioLedger parameter names
    # e.g., ${GALAXY_SLOTS:-4} or \${GALAXY_SLOTS:-4} in param value
    # → map GALAXY_SLOTS → threads
    env_var_map: dict[str, str] = {}
    for param in root.findall(".//inputs/param"):
        raw_value = param.get("value", "")
        name = param.get("name", "")
        if not name:
            arg = param.get("argument", "")
            name = arg.lstrip("-").replace("-", "_") if arg else ""

        # Handle both ${VAR} and \${VAR} patterns (Galaxy XML escaping)
        if raw_value.endswith("}"):
            if raw_value.startswith("${"):
                inner = raw_value[2:-1]  # Remove ${ and }
            elif raw_value.startswith("\\${"):
                inner = raw_value[3:-1]  # Remove \${ and }
            else:
                continue

            var_name = inner.split(":-")[0].split(":")[0]
            if var_name and name:
                env_var_map[var_name] = name

    # Extract command and convert Cheetah → Jinja2
    command_el = root.find("command")
    raw_command = (command_el.text or "").strip() if command_el is not None else ""

    # Convert Galaxy Cheetah template to BioLedger Jinja2
    input_names = set(inputs.keys())
    param_names = set(parameters.keys())
    command = _convert_cheetah_to_jinja2(
        raw_command, input_names, param_names, env_var_map
    )

    # Resolve version: only use the literal Galaxy version. If it contains
    # unresolved macros we leave it empty and warn — we do NOT guess versions
    # from biocontainers. The user must set the correct version manually.
    raw_version = root.get("version", "")
    if has_macro(raw_version):
        unresolved_macros.append(
            f"version contains unresolved macros: {raw_version}; "
            "set the correct version manually"
        )
    version = _resolve_version_for_pkg(raw_version)

    spec = ExecutionSpec(
        name=root.get("id", ""),
        version=version,
        description=(root.findtext("description") or "").strip(),
        container=container,
        command=command,
        inputs=inputs,
        outputs=outputs,
        parameters=parameters,
        status=SpecStatus.DRAFT,  # imported specs start as drafts
    )
    return GalaxyParseResult(spec=spec, warnings=unresolved_macros)


def from_galaxy_xml(xml_str: str) -> ExecutionSpec:
    """Parse a Galaxy tool XML wrapper into a BioLedger ExecutionSpec.

    Back-compat shim around :func:`parse_galaxy_xml` that prints unresolved
    macros directly to the console. Prefer :func:`parse_galaxy_xml` from new
    code so the caller controls how warnings are surfaced.
    """
    result = parse_galaxy_xml(xml_str)
    if result.warnings:
        _print_galaxy_warnings(result.warnings)
    return result.spec


def _print_galaxy_warnings(warnings: list[str]) -> None:
    """Render Galaxy-import warnings as a rich Panel."""
    console.print(
        Panel.fit(
            "[yellow]Warning: Unresolved Galaxy macros detected:\n"
            + "\n".join(f"  • {m}" for m in warnings)
            + "\n\nThese values are tool-specific and require manual review. "
            "Check the generated spec and update versions/container as needed.",
            title="Galaxy Import",
            border_style="yellow",
        )
    )


# Patterns that signal Galaxy/Cheetah residue surviving programmatic conversion.
# Used to gate the LLM ``fix_galaxy_import`` step.
_RESIDUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bare $var reference", re.compile(r"(?<!\{)\$\w+")),
    (
        "Cheetah directive",
        re.compile(
            r"(^|\s)#(set|if|elif|else|end if|endif|for|end for|import|echo|silent)\b"
        ),
    ),
    (".element_identifier", re.compile(r"\.element_identifier\b")),
    (".files_path", re.compile(r"\.files_path\b")),
    (".dataset", re.compile(r"\.dataset\b")),
    ("re.sub call", re.compile(r"\bre\.sub\(")),
    (".endswith on rendered template", re.compile(r"\}\}\s*\.endswith")),
)


def _galaxy_residue(command: str) -> list[str]:
    """Return human-readable descriptions of any Galaxy/Cheetah leftovers."""
    if not command:
        return []
    found: list[str] = []
    for label, pattern in _RESIDUE_PATTERNS:
        if pattern.search(command):
            found.append(label)
    return found


async def import_galaxy_tool(
    path: Path,
    deps: ForgeDeps,
    agent: "ToolForgeAgent",  # noqa: F821
    use_llm: bool = True,
    context: GalaxyImportContext | None = None,
) -> ToolSpec:
    """Import a Galaxy tool XML into a BioLedger ToolSpec.

    Accepts a file or directory path. If a directory, resolves macros.xml
    and auxiliary scripts automatically.

    Pipeline:
        1. Resolve context (file vs directory, macros, assets).
        2. Expand macros programmatically (if macros.xml found).
        3. Programmatic parse (``parse_galaxy_xml``).
        4. Programmatic validation.
        5. Single LLM fix step — broad scope, only when needed.
    """
    context = context or resolve_import_context(path)
    xml_str = context.xml_path.read_text()
    warnings: list[str] = []

    # Step 1: Macro expansion (programmatic)
    macro_set = None
    if context.macros_xml_path:
        macro_set = load_macros(context.macros_xml_path)
        xml_str = expand_xml_macros(xml_str, macro_set.xml_macros)

    # Build known_values from macros.xml tokens + explicit XML values.
    # We use values that are stated in the source — no guessing.
    known_values: dict[str, str] = {}
    if macro_set:
        known_values.update(macro_set.tokens)
    try:
        root = ET.fromstring(xml_str)
        for req in root.findall(".//requirements/requirement"):
            if req.get("type") == "package":
                pkg = (req.text or "").strip()
                if pkg and not has_macro(pkg):
                    known_values[pkg.upper().replace("-", "_")] = pkg
    except ET.ParseError:
        pass

    xml_str, unresolved_macros = expand_text_macros(xml_str, known_values)
    for m in unresolved_macros:
        warnings.append(f"unresolved macro: @{m}@")

    # Step 2: Programmatic translation
    try:
        parse_result = parse_galaxy_xml(xml_str)
        exec_spec = parse_result.spec
        warnings.extend(parse_result.warnings)
    except Exception as e:
        if not use_llm:
            raise
        with console.status("[cyan]LLM fallback parse…[/cyan]"):
            exec_spec = await agent.parse(xml_str, "Galaxy XML", str(e), deps)

    if warnings:
        _print_galaxy_warnings(warnings)

    # Step 3: Programmatic validation
    result = validate_execution(exec_spec, strict=False)

    # Step 4: Single LLM fix step — broad scope
    has_validation_issues = not result.is_valid
    has_warnings = bool(warnings)
    residue = _galaxy_residue(exec_spec.command)
    has_residue = bool(residue)

    if use_llm and (has_validation_issues or has_warnings or has_residue):
        reasons = []
        if has_validation_issues:
            reasons.append(f"{len(result.issues)} validation issue(s)")
        if has_warnings:
            reasons.append(f"{len(warnings)} warning(s)")
        if has_residue:
            reasons.append("Galaxy residue detected")
        console.print(f"  [dim]{' ,'.join(reasons)} — running LLM fix…[/dim]")

        # Lock the container — the LLM tends to hallucinate build hashes.
        # The programmatic biocontainers resolution is always more reliable.
        pre_llm_container = exec_spec.container

        with console.status("[cyan]LLM fixing import issues…[/cyan]"):
            fixed_spec, changes_made, remaining_issues = await agent.fix_galaxy_import(
                exec_spec, xml_str, deps,
                parser_warnings=warnings,
                residue=residue,
                validation_issues=result.issues if has_validation_issues else [],
            )
        exec_spec = fixed_spec

        # Always revert container changes from LLM — it hallucinates build hashes
        if exec_spec.container != pre_llm_container:
            console.print(
                "  [dim]Reverting LLM container change "
                f"({exec_spec.container} → {pre_llm_container})[/dim]"
            )
            exec_spec.container = pre_llm_container

        if changes_made:
            console.print("  [green]LLM fixes applied:[/green]")
            for change in changes_made:
                console.print(f"    ✓ {change}")

        if remaining_issues:
            console.print("  [yellow]Remaining issues (manual review needed):[/yellow]")
            for issue in remaining_issues:
                console.print(f"    ! {issue}")

        # Re-validate after LLM fix
        result = validate_execution(exec_spec, strict=False)

    return ToolSpec(execution=exec_spec)


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
