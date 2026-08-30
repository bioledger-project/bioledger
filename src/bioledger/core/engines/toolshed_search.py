"""Galaxy Tool Shed search and download.

Searches the main Galaxy Tool Shed (https://toolshed.g2.bx.psu.edu) for tools
matching a query, downloads tool XML + macros to a local cache directory, and
parses the downloaded XML into a minimal ExecutionSpec for the Galaxy adapter.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    ToolInput,
    ToolOutput,
)

logger = logging.getLogger(__name__)

TOOLSHED_URL = "https://toolshed.g2.bx.psu.edu"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "toolshed"


@dataclass
class ToolShedResult:
    """A single Tool Shed search hit."""

    tool_id: str
    name: str
    description: str
    repo_owner: str
    repo_name: str


def search_toolshed(query: str, page_size: int = 10) -> list[ToolShedResult]:
    """Search the main Galaxy Tool Shed via bioblend.

    Returns a list of ToolShedResult objects matching the query.
    """
    try:
        from bioblend.toolshed import ToolShedInstance
    except ImportError:
        raise RuntimeError(
            "bioblend is not installed. "
            "Install with: pip install bioledger[galaxy]"
        )

    ts = ToolShedInstance(url=TOOLSHED_URL)
    raw_results = ts.tools.search_tools(query, page_size=page_size)

    results: list[ToolShedResult] = []
    for hit in raw_results:
        # bioblend returns dicts with varying keys depending on API version
        name = hit.get("name", "")
        description = hit.get("description", "")
        repo_owner = hit.get("repo_owner", hit.get("owner", ""))
        repo_name = hit.get("repo_name", name)
        tool_id = hit.get("id", f"{repo_owner}/{repo_name}")
        results.append(
            ToolShedResult(
                tool_id=tool_id,
                name=name,
                description=description,
                repo_owner=repo_owner,
                repo_name=repo_name,
            )
        )
    return results


def download_tool(
    repo_owner: str,
    repo_name: str,
    revision: str | None = None,
    dest_dir: Path | None = None,
) -> tuple[Path, str]:
    """Download tool XML + macros.xml + scripts to cache dir.

    Uses ts.repositories.get_repository_revision_install_info() to get the
    file list, then downloads each file via the Tool Shed API.

    Returns tuple of (path to downloaded tool directory, resolved changeset
    revision).
    """
    try:
        from bioblend.toolshed import ToolShedInstance
    except ImportError:
        raise RuntimeError(
            "bioblend is not installed. "
            "Install with: pip install bioledger[galaxy]"
        )

    ts = ToolShedInstance(url=TOOLSHED_URL)
    dest = dest_dir or (_CACHE_DIR / repo_owner / repo_name)
    dest.mkdir(parents=True, exist_ok=True)

    # Get repository revisions
    if revision:
        revisions = [revision]
    else:
        repo_info = ts.repositories.show_repository(
            f"{repo_owner}/{repo_name}"
        )
        revisions = [
            r.get("changeset_revision", "")
            for r in repo_info.get("repo_revisions", [])
        ]
        if not revisions:
            # Fall back to latest
            revisions = [repo_info.get("tip_revision", "")]

    rev = revisions[0] if revisions else ""

    # Get file list for this revision
    install_info = ts.repositories.get_repository_revision_install_info(
        f"{repo_owner}/{repo_name}",
        rev,
    )

    # Download files
    import httpx

    for file_info in install_info:
        file_path = file_info.get("file_path", "")
        if not file_path:
            continue
        url = (
            f"{TOOLSHED_URL}/repository/download?"
            f"repository_id={repo_owner}/{repo_name}"
            f"&changeset_revision={rev}&file={file_path}"
        )
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code == 200:
                local_path = dest / Path(file_path).name
                local_path.write_bytes(resp.content)
        except Exception as e:
            logger.warning("Failed to download %s: %s", file_path, e)

    return dest, rev


def parse_tool_to_spec(tool_dir: Path) -> ExecutionSpec:
    """Parse a downloaded Galaxy tool XML into a minimal ExecutionSpec.

    Extracts name, container, command, inputs, outputs — enough for generate()
    to produce a valid .ga workflow. This is a lightweight parser for
    discovered tools, not a full translator.
    """
    # Find the main tool XML file
    xml_files = list(tool_dir.glob("*.xml"))
    if not xml_files:
        raise FileNotFoundError(f"No .xml files found in {tool_dir}")

    # Use the first XML that has a <tool> root element
    tree = None
    for xml_path in xml_files:
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            if root.tag == "tool":
                break
        except ET.ParseError:
            continue
    if tree is None:
        raise ValueError(f"No valid Galaxy tool XML found in {tool_dir}")

    root = tree.getroot()
    tool_id = root.get("id", root.get("name", "unknown"))
    version = root.get("version", "")

    # Extract description
    description = (root.findtext("description") or "").strip()

    # Extract container from requirements
    container = ""
    requirements = root.find("requirements")
    if requirements is not None:
        for req in requirements.findall("requirement"):
            if req.get("type") == "docker":
                container = req.text or ""
                break

    # Extract command — no longer attempts Cheetah-to-Jinja translation.
    # The command field is set by submit_discovered() to a planemo invocation;
    # the raw Cheetah template is not useful for direct execution.
    command = ""

    # Extract inputs from <inputs><param> elements
    inputs: dict[str, ToolInput] = {}
    inputs_elem = root.find("inputs")
    if inputs_elem is not None:
        for param in inputs_elem.findall("param"):
            inp_name = param.get("name", "")
            if not inp_name:
                continue
            inp_type = param.get("type", "text")
            inp_format = "any"
            if inp_type == "data":
                inp_format = param.get("format", "any")
            inputs[inp_name] = ToolInput(
                name=inp_name,
                type=ParamType.FILE,
                format=inp_format,
            )

    # Extract outputs from <outputs><data> elements
    outputs: dict[str, ToolOutput] = {}
    outputs_elem = root.find("outputs")
    if outputs_elem is not None:
        for data in outputs_elem.findall("data"):
            out_name = data.get("name", "")
            if not out_name:
                continue
            out_format = data.get("format", "any")
            # Only use from_work_dir as pattern — label is human-readable
            # text, not a valid filesystem glob.
            out_pattern = data.get("from_work_dir", "")
            outputs[out_name] = ToolOutput(
                name=out_name,
                type=ParamType.FILE,
                format=out_format,
                pattern=out_pattern,
            )

    # command is left empty — submit_discovered() sets it to the planemo
    # invocation. The raw Cheetah template is not portable to Jinja2.

    return ExecutionSpec(
        name=tool_id,
        version=version,
        description=description,
        container=container,
        command=command,
        inputs=inputs,
        outputs=outputs,
        executors=["galaxy"],
    )
