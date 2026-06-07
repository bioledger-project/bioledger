from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Galaxy-specific tokens that are inert in BioLedger and should be stripped.
# These handle Galaxy job setup/teardown that BioLedger doesn't need.
_GALAXY_INERT_TOKENS = {
    "SYMLINK_FILES",
    "SHELL_OPTIONS",
    "ERRORS",
}


@dataclass
class MacroSet:
    """Parsed macros.xml contents: XML fragments and text tokens."""

    xml_macros: dict[str, ET.Element] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)


def load_macros(macros_xml_path: Path) -> MacroSet:
    """Parse macros.xml and extract both XML macros and text tokens.

    Galaxy macros.xml uses two mechanisms:
      <token name="@TOOL_VERSION@">2.5.38</token>   → text substitution
      <xml name="requirements">...</xml>             → XML fragment for <expand>
      <macro name="requirements">...</macro>         → same as <xml>
    """
    tree = ET.parse(macros_xml_path)
    root = tree.getroot()

    result = MacroSet()
    for token_el in root.findall(".//token"):
        name = token_el.get("name", "")
        clean_name = name.strip("@")
        if clean_name and token_el.text:
            result.tokens[clean_name] = token_el.text.strip()

    # Galaxy uses both <xml name="..."> and <macro name="..."> for fragments
    for el in root.findall(".//xml"):
        name = el.get("name")
        if name:
            result.xml_macros[name] = el

    for macro_el in root.findall(".//macro"):
        name = macro_el.get("name")
        if name:
            result.xml_macros[name] = macro_el

    return result


def expand_xml_macros(xml_str: str, macros: dict[str, ET.Element]) -> str:
    """Replace <expand macro="name"/> tags with actual macro content."""
    if not macros:
        return xml_str

    root = ET.fromstring(xml_str)

    for parent in root.iter():
        children_to_replace = []
        for i, child in enumerate(list(parent)):
            if child.tag == "expand":
                macro_name = child.get("macro")
                if macro_name and macro_name in macros:
                    children_to_replace.append((i, child, macros[macro_name]))

        for i, expand_el, macro_content in reversed(children_to_replace):
            macro_children = list(macro_content)
            if macro_children:
                for j, mc in enumerate(macro_children):
                    parent.insert(i + j, mc)
            else:
                cloned = ET.Element(
                    macro_content.tag, attrib=macro_content.attrib
                )
                cloned.text = macro_content.text
                cloned.tail = macro_content.tail
                parent.insert(i, cloned)
            parent.remove(expand_el)

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def expand_text_macros(
    xml_str: str, known_values: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    """Replace @MACRO@ tokens where values are known.

    Known Galaxy-specific inert tokens (e.g. @SYMLINK_FILES@) are replaced
    with empty strings automatically.

    Returns (expanded_xml, list_of_unresolved_macro_names).
    """
    known_values = known_values or {}
    unresolved: list[str] = []

    def replace_macro(match: re.Match) -> str:
        macro_name = match.group(1)
        if macro_name in known_values:
            return known_values[macro_name]
        if macro_name in _GALAXY_INERT_TOKENS:
            return ""
        unresolved.append(macro_name)
        return match.group(0)

    pattern = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)@")
    expanded = pattern.sub(replace_macro, xml_str)

    return expanded, list(set(unresolved))
