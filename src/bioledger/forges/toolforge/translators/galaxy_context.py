from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GalaxyImportContext:
    """Everything the importer needs to know about the source location."""

    xml_path: Path
    macros_xml_path: Path | None = None
    assets: list[Path] = field(default_factory=list)
    base_dir: Path | None = None


@dataclass
class GalaxySuiteContext:
    """A tool suite: multiple tool XMLs sharing one macros.xml."""

    xml_paths: list[Path]
    macros_xml_path: Path | None = None
    base_dir: Path | None = None


# Common script extensions referenced inside <command> / <configfiles>
_SCRIPT_EXTENSIONS = {".py", ".r", ".R", ".sh", ".pl", ".rb"}

# Regex to find file-like references in command text
_SCRIPT_REFS = "|".join(re.escape(ext) for ext in _SCRIPT_EXTENSIONS)
_SCRIPT_REF = re.compile(r"[\w./-]+(?:" + _SCRIPT_REFS + r")\b")


def resolve_import_context(path: Path) -> GalaxyImportContext | GalaxySuiteContext:
    """Figure out what we got: a single XML file, a tool directory, or a tool suite.

    For a file: returns minimal context (just the xml_path).
    For a directory with one tool XML: returns single context.
    For a directory with multiple tool XMLs: returns suite context.
    """
    if path.is_file():
        return GalaxyImportContext(xml_path=path, base_dir=path.parent)

    if not path.is_dir():
        raise FileNotFoundError(f"Path does not exist: {path}")

    tool_xmls = _find_all_tool_xml(path)
    macros_xml_path = _find_macros_xml(path)

    if not tool_xmls:
        raise ValueError(f"No Galaxy tool XML found in {path}")

    if len(tool_xmls) == 1:
        assets = _find_assets(path, tool_xmls[0])
        return GalaxyImportContext(
            xml_path=tool_xmls[0],
            macros_xml_path=macros_xml_path,
            assets=assets,
            base_dir=path,
        )

    return GalaxySuiteContext(
        xml_paths=tool_xmls,
        macros_xml_path=macros_xml_path,
        base_dir=path,
    )


def _find_all_tool_xml(directory: Path) -> list[Path]:
    """Find all Galaxy tool XMLs in a directory.

    Looks for *.xml files with a <tool> root element, excluding macros.xml.
    """
    candidates: list[Path] = []
    for xml_file in sorted(directory.glob("*.xml")):
        if xml_file.name.lower() == "macros.xml":
            continue
        try:
            root = ET.parse(xml_file).getroot()
            if root.tag == "tool":
                candidates.append(xml_file)
        except ET.ParseError:
            continue

    return candidates


def _find_macros_xml(directory: Path) -> Path | None:
    """Look for macros.xml in the same directory as the tool XML."""
    macros = directory / "macros.xml"
    return macros if macros.is_file() else None


def _find_assets(directory: Path, xml_path: Path) -> list[Path]:
    """Find auxiliary scripts referenced in the tool XML.

    Scans <command> and <configfiles> for file references that resolve to
    actual files in the directory.
    """
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return []

    text_parts: list[str] = []
    for el in root.iter():
        if el.tag in ("command", "configfiles") or el.get("file") is not None:
            if el.text:
                text_parts.append(el.text)
            for child in el:
                if child.text:
                    text_parts.append(child.text)

    found: list[Path] = []
    seen: set[Path] = set()
    for part in text_parts:
        for match in _SCRIPT_REF.finditer(part):
            ref = match.group(0)
            # Strip Galaxy __tool_directory__/ prefix (means "same dir as tool XML")
            ref = ref.lstrip("/")
            while ref.startswith("__tool_directory__/"):
                ref = ref[len("__tool_directory__/"):]
            # Resolve relative to the directory
            candidate = (directory / ref).resolve()
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                found.append(candidate)

    return found
