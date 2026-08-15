"""nf-core modules search and download.

Searches the nf-core/modules GitHub repository for modules matching a query,
downloads module files to a local cache directory, and parses the downloaded
Nextflow DSL2 process into a minimal ExecutionSpec for the Nextflow adapter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    ToolInput,
    ToolOutput,
)

logger = logging.getLogger(__name__)

NFCORE_MODULES_REPO = "nf-core/modules"
NFCORE_GITHUB_API = "https://api.github.com/repos/nf-core/modules"
NFCORE_GITHUB_RAW = "https://raw.githubusercontent.com/nf-core/modules"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "nfcore"


@dataclass
class NFCoreResult:
    """A single nf-core module search hit."""

    name: str
    description: str
    subdirectory: str  # e.g. "align/bowtie2/align"


def search_nfcore(query: str) -> list[NFCoreResult]:
    """Search nf-core/modules for modules matching the query.

    Uses the GitHub API to list directories under modules/nf-core/,
    then filters by name match.
    """
    import httpx

    # List directories under modules/nf-core/
    api_url = f"{NFCORE_GITHUB_API}/contents/modules/nf-core"
    resp = httpx.get(api_url, timeout=30.0)
    if resp.status_code != 200:
        logger.warning("GitHub API returned %d", resp.status_code)
        return []

    categories = resp.json()
    results: list[NFCoreResult] = []

    for category in categories:
        if category.get("type") != "dir":
            continue
        cat_name = category.get("name", "")
        # List tools under this category
        cat_url = f"{NFCORE_GITHUB_API}/contents/modules/nf-core/{cat_name}"
        cat_resp = httpx.get(cat_url, timeout=30.0)
        if cat_resp.status_code != 200:
            continue
        tools = cat_resp.json()
        for tool in tools:
            if tool.get("type") != "dir":
                continue
            tool_name = tool.get("name", "")
            full_name = f"{cat_name}/{tool_name}"
            # Check if query matches
            if query.lower() in full_name.lower() or query.lower() in tool_name.lower():
                # Try to get description from meta.yml
                subdir = f"{cat_name}/{tool_name}"
                results.append(
                    NFCoreResult(
                        name=tool_name,
                        description=_fetch_description(subdir),
                        subdirectory=subdir,
                    )
                )

    return results


def _fetch_description(subdir: str) -> str:
    """Fetch the description from the module's meta.yml."""
    import httpx

    url = f"{NFCORE_GITHUB_RAW}/main/modules/nf-core/{subdir}/meta.yml"
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            # Simple YAML parsing — just look for description field
            for line in resp.text.splitlines():
                if line.strip().startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


def download_module(
    subdirectory: str,
    revision: str = "main",
    dest_dir: Path | None = None,
) -> Path:
    """Download an nf-core module's main.nf and meta.yml to cache dir.

    Returns path to the downloaded module directory.
    """
    import httpx

    dest = dest_dir or (_CACHE_DIR / subdirectory.replace("/", "_"))
    dest.mkdir(parents=True, exist_ok=True)

    files = ["main.nf", "meta.yml"]
    for fname in files:
        url = f"{NFCORE_GITHUB_RAW}/{revision}/modules/nf-core/{subdirectory}/{fname}"
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code == 200:
                (dest / fname).write_text(resp.text)
        except Exception as e:
            logger.warning("Failed to download %s: %s", fname, e)

    return dest


def parse_meta_yml(module_dir: Path) -> dict:
    """Parse a downloaded nf-core module's meta.yml for input/output metadata.

    Returns a dict with keys:
    - ``inputs``: list of input names (for channel wiring)
    - ``outputs``: dict of {name: pattern} (for provenance)
    - ``container``: container image string (may be empty)

    Unlike ``parse_module_to_spec``, this reads the structured ``meta.yml``
    file rather than regex-parsing ``main.nf``, making it robust to complex
    DSL2 patterns (tuple inputs, meta maps, config objects).
    """
    import re

    meta_path = module_dir / "meta.yml"
    if not meta_path.exists():
        logger.warning("meta.yml not found in %s", module_dir)
        return {"inputs": [], "outputs": {}, "container": ""}

    content = meta_path.read_text()

    # Simple YAML parsing — no yaml dependency needed for the flat structure
    # of nf-core meta.yml files.
    inputs: list[str] = []
    outputs: dict[str, str] = {}
    container = ""

    # Parse input names from the 'input' section
    in_inputs = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("input:"):
            in_inputs = True
            continue
        if in_inputs:
            # End of input section when we hit a new top-level key
            _section_keys = ("-", "name:", "description:", "type:", "pattern:")
            if stripped and not stripped.startswith(_section_keys):
                in_inputs = False
            elif stripped.startswith("- name:"):
                name = stripped.split(":", 1)[1].strip().strip("\"'")
                if name:
                    inputs.append(name)

    # Parse output names and patterns from the 'output' section
    in_outputs = False
    current_output_name = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("output:"):
            in_outputs = True
            continue
        if in_outputs:
            _section_keys = ("-", "name:", "description:", "type:", "pattern:")
            if stripped and not stripped.startswith(_section_keys):
                in_outputs = False
            elif stripped.startswith("- name:"):
                current_output_name = stripped.split(":", 1)[1].strip().strip("\"'")
            elif stripped.startswith("pattern:") and current_output_name:
                pattern = stripped.split(":", 1)[1].strip().strip("\"'")
                outputs[current_output_name] = pattern
                current_output_name = ""

    # Try to extract container from main.nf (meta.yml doesn't have it)
    nf_path = module_dir / "main.nf"
    if nf_path.exists():
        nf_content = nf_path.read_text()
        container_match = re.search(
            r"container\s+['\"]([^'\"]+)['\"]", nf_content,
        )
        if container_match:
            container = container_match.group(1)

    return {
        "inputs": inputs,
        "outputs": outputs,
        "container": container,
    }


def parse_module_to_spec(module_dir: Path) -> ExecutionSpec:
    """Parse a downloaded nf-core module main.nf into a minimal ExecutionSpec.

    Extracts process name, container, inputs, outputs — enough for
    submit_spec() to generate a Nextflow workflow.
    """
    nf_path = module_dir / "main.nf"
    if not nf_path.exists():
        raise FileNotFoundError(f"main.nf not found in {module_dir}")

    content = nf_path.read_text()

    # Extract process name
    name_match = re.search(r"process\s+(\w+)\s*\{", content)
    name = name_match.group(1) if name_match else "unknown"

    # Extract container
    container = ""
    container_match = re.search(r"container\s+['\"]([^'\"]+)['\"]", content)
    if container_match:
        container = container_match.group(1)

    # Extract input declarations
    inputs: dict[str, ToolInput] = {}
    input_block = re.search(
        r"input:\s*\n(.*?)(?=\n\s*(output:|script:|shell:|exec:))",
        content,
        re.DOTALL,
    )
    if input_block:
        for line in input_block.group(1).strip().splitlines():
            line = line.strip()
            # Match "path <name>" or "tuple val(x), path(y)" patterns
            path_match = re.search(r"path\s*\(?(\w+)\)?", line)
            if path_match:
                inp_name = path_match.group(1)
                inputs[inp_name] = ToolInput(
                    name=inp_name,
                    type=ParamType.FILE,
                    format="any",
                )

    # Extract output declarations
    outputs: dict[str, ToolOutput] = {}
    output_block = re.search(
        r"output:\s*\n(.*?)(?=\n\s*(script:|shell:|exec:))",
        content,
        re.DOTALL,
    )
    if output_block:
        for line in output_block.group(1).strip().splitlines():
            line = line.strip()
            # Match 'path "pattern", emit: <name>'
            emit_match = re.search(r"emit:\s*(\w+)", line)
            if emit_match:
                out_name = emit_match.group(1)
                pattern_match = re.search(r'["\']([^"\']+)["\']', line)
                pattern = pattern_match.group(1) if pattern_match else ""
                outputs[out_name] = ToolOutput(
                    name=out_name,
                    type=ParamType.FILE,
                    format="any",
                    pattern=pattern,
                )

    # Build a minimal command — the actual script is in the process block
    # For ExecutionSpec, we use a placeholder that references inputs
    first_input = list(inputs.keys())[0] if inputs else "input"
    command = f"{name} {{{{ inputs.{first_input} }}}}"

    return ExecutionSpec(
        name=name,
        version="",
        description="",
        container=container,
        command=command,
        inputs=inputs,
        outputs=outputs,
        executors=["nextflow"],
    )
