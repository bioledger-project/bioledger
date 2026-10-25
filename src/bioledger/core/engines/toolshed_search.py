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
    ToolParameter,
)

logger = logging.getLogger(__name__)

TOOLSHED_URL = "https://toolshed.g2.bx.psu.edu"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "toolshed"


def find_main_tool_xml(tool_dir: Path) -> Path:
    """Return the first ``*.xml`` under ``tool_dir`` whose root element is ``<tool>``.

    Tool Shed repositories ship tool XMLs under their real names
    (``bowtie2.xml``), plus non-tool XML files (``macros.xml``, datatype
    configs). Raises ``FileNotFoundError`` if none is found.
    """
    for xml in sorted(Path(tool_dir).rglob("*.xml")):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        if root.tag == "tool":
            return xml
    raise FileNotFoundError(f"No Galaxy <tool> XML found under {tool_dir}")


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
    """Download expanded tool XML for a repository revision.

    Tool Shed 2.0 removed the legacy ``/repository/download`` endpoint
    (404) and blocks Mercurial archive/raw-file URLs (403). The live API
    serves each tool's *expanded* document — macros already inlined — via
    ``/api/tools/{owner}~{repo}~{tool_id}/versions/{version}/tool_source``.

    Note: arbitrary repository files (scripts, test data) are NOT
    downloadable via the Tool Shed 2.0 API — only the tool XML itself.

    Returns tuple of (path to downloaded tool directory, resolved changeset
    revision).
    """
    import httpx

    dest = dest_dir or (_CACHE_DIR / repo_owner / repo_name)
    dest.mkdir(parents=True, exist_ok=True)

    # 1. Resolve the changeset revision (pinned or latest installable).
    if revision:
        rev = revision
    else:
        resp = httpx.get(
            f"{TOOLSHED_URL}/api/repositories/get_ordered_installable_revisions",
            params={"owner": repo_owner, "name": repo_name},
            timeout=30.0,
        )
        resp.raise_for_status()
        revs = resp.json()
        if not revs:
            raise RuntimeError(
                f"No installable revisions for {repo_owner}/{repo_name}"
            )
        rev = revs[-1]

    # 2. Enumerate valid tools at this revision. install_info is a
    # 3-tuple: [repo, metadata, tools]; metadata.valid_tools carries the
    # tool id and a guid whose last segment is the tool version.
    resp = httpx.get(
        f"{TOOLSHED_URL}/api/repositories/get_repository_revision_install_info",
        params={
            "name": repo_name,
            "owner": repo_owner,
            "changeset_revision": rev,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    info = resp.json()
    valid_tools = (
        info[1].get("valid_tools", [])
        if isinstance(info, list) and len(info) > 1
        else []
    )
    if not valid_tools:
        raise RuntimeError(
            f"Revision {rev} of {repo_owner}/{repo_name} declares no valid tools"
        )

    # 3. Download each tool's expanded document.
    downloaded = 0
    for tool in valid_tools:
        tool_id = tool.get("id")
        guid = tool.get("guid", "")
        tool_version = guid.rsplit("/", 1)[-1] if "/" in guid else ""
        if not tool_id or not tool_version:
            continue
        ts_id = f"{repo_owner}~{repo_name}~{tool_id}"
        url = (
            f"{TOOLSHED_URL}/api/tools/{ts_id}"
            f"/versions/{tool_version}/tool_source"
        )
        try:
            tool_resp = httpx.get(url, timeout=30.0)
            if tool_resp.status_code == 200:
                (dest / f"{tool_id}.xml").write_text(tool_resp.text)
                downloaded += 1
            else:
                logger.warning(
                    "tool_source %s returned %d", ts_id, tool_resp.status_code,
                )
        except Exception as e:
            logger.warning("Failed to download %s: %s", ts_id, e)

    if downloaded == 0:
        raise RuntimeError(
            f"Failed to download any tool XML for {repo_owner}/{repo_name}@{rev}"
        )

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
            if req.get("type") in ("docker", "container"):
                container = req.text or ""
                break

    # Extract command — no longer attempts Cheetah-to-Jinja translation.
    # The command field is set by submit_discovered() to a planemo invocation;
    # the raw Cheetah template is not useful for direct execution.
    command = ""

    # Extract inputs + parameters from <inputs>. Galaxy nests most real
    # params inside <conditional>/<when>/<repeat>/<section> — walk them
    # recursively, using Galaxy's own `parent|child` naming (seen in
    # valid_tools test specs like "library|input_1"). Only type="data"
    # (and data_collection) params are files; everything else is a
    # ToolParameter — treating them all as FILE poisoned job.yml.
    inputs: dict[str, ToolInput] = {}
    parameters: dict[str, ToolParameter] = {}

    def _walk_params(elem: ET.Element, prefix: str = ""):
        for child in elem:
            if child.tag == "param":
                name = child.get("name", "")
                if name:
                    yield f"{prefix}{name}", child
            elif child.tag == "conditional":
                cond_name = child.get("name", "")
                for sub in child:
                    if sub.tag == "param" and sub.get("name"):
                        yield f"{prefix}{cond_name}|{sub.get('name')}", sub
                    elif sub.tag == "when":
                        yield from _walk_params(sub, f"{prefix}{cond_name}|")
            elif child.tag in ("repeat", "section"):
                yield from _walk_params(
                    child, f"{prefix}{child.get('name', '')}|",
                )

    _param_types = {
        "integer": ParamType.INTEGER,
        "float": ParamType.FLOAT,
        "boolean": ParamType.BOOLEAN,
        "select": ParamType.SELECT,
    }
    inputs_elem = root.find("inputs")
    if inputs_elem is not None:
        for pname, param in _walk_params(inputs_elem):
            ptype = param.get("type", "text")
            if ptype in ("data", "data_collection"):
                inputs[pname] = ToolInput(
                    name=pname,
                    type=ParamType.FILE,
                    format=param.get("format", "any"),
                )
            else:
                options = [
                    opt.get("value", "")
                    for opt in param.findall("option")
                ] if ptype == "select" else None
                parameters[pname] = ToolParameter(
                    name=pname,
                    type=_param_types.get(ptype, ParamType.STRING),
                    default=param.get("value") or None,
                    required=False,
                    options=options or None,
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
        parameters=parameters,
        executors=["galaxy"],
    )
