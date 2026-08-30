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

    .. deprecated:: Use :func:`install_nfcore_module` instead, which uses
       ``nf-core modules install`` for dependency-aware installation.

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


def install_nfcore_module(
    subdirectory: str,
    dest_dir: Path,
    revision: str | None = None,
) -> tuple[Path, str]:
    """Install an nf-core module using ``nf-core modules install``.

    Creates a minimal ``.nf-core.yml`` in ``dest_dir`` to suppress interactive
    prompts, then runs ``nf-core modules install <subdir> --dir <dest_dir>
    --force``. If ``revision`` is provided, adds ``--sha <revision>``.

    Args:
        subdirectory: Module path (e.g. "align/bowtie2/align").
        dest_dir: Directory to install into (created if needed).
        revision: Optional git SHA or tag to pin.

    Returns:
        Tuple of (module_dir, resolved_sha). ``module_dir`` is the path to
        the installed module's directory. ``resolved_sha`` is the SHA from
        ``modules.json`` after installation.
    """
    import json as _json
    import shutil as _shutil
    import subprocess as _subprocess

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Create minimal .nf-core.yml to avoid interactive prompts
    nfcore_yml = dest_dir / ".nf-core.yml"
    if not nfcore_yml.exists():
        nfcore_yml.write_text("repository_type: modules\n")

    # Build the install command
    nf_core_bin = _shutil.which("nf-core") or "nf-core"
    cmd = [
        nf_core_bin, "modules", "install",
        subdirectory,
        "--dir", str(dest_dir),
        "--force",
    ]
    if revision:
        cmd.extend(["--sha", revision])

    result = _subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"nf-core modules install failed for {subdirectory}: "
            f"{result.stderr or result.stdout}"
        )

    # Read modules.json to get the resolved SHA
    modules_json_path = dest_dir / "modules.json"
    resolved_sha = ""
    if modules_json_path.exists():
        try:
            modules_data = _json.loads(modules_json_path.read_text())
            repos = modules_data.get("repos", {})
            nfcore_repo = repos.get("nf-core/modules", {})
            modules = nfcore_repo.get("modules", {})
            # The subdirectory in modules.json is relative to modules/nf-core/
            # e.g. "align/bowtie2/align"
            module_entry = modules.get(subdirectory, {})
            resolved_sha = module_entry.get("git_sha", "")
        except Exception as e:
            logger.warning("Failed to read modules.json: %s", e)

    # The module files are installed under modules/nf-core/<subdirectory>/
    module_dir = dest_dir / "modules" / "nf-core" / subdirectory
    if not module_dir.exists():
        # Fallback: try to find the module directory
        logger.warning(
            "Expected module dir %s not found, searching...", module_dir,
        )
        for p in dest_dir.rglob("main.nf"):
            if subdirectory.split("/")[-1] in str(p):
                module_dir = p.parent
                break

    return module_dir, resolved_sha


def generate_nfcore_wrapper(
    module_subdir: str,
    module_dir: Path,
    meta: dict,
    entry_id: str,
) -> str:
    """Generate a wrapper main.nf for an nf-core module.

    The wrapper includes the module process, creates channels from params,
    and sets ``publishDir`` to ``params.outdir`` so outputs land in the
    work directory.

    Args:
        module_subdir: The nf-core subdirectory (e.g. "align/bowtie2/align").
        module_dir: Path to the installed module directory.
        meta: Parsed meta.yml dict (from ``parse_meta_yml``).
        entry_id: The ledger entry ID, used for the include path.

    Returns:
        The wrapper main.nf content as a string.
    """
    module_name = module_subdir.split("/")[-1]
    input_names = meta.get("inputs", [])
    output_names = list(meta.get("outputs", {}).keys())

    # Build include directive — relative to ${projectDir}/<entry_id>/
    # The module files are in companion_files under the entry's namespace
    include_path = f"modules/nf-core/{module_subdir}/main"

    # Build channel declarations
    channel_lines = []
    for name in input_names:
        channel_lines.append(
            f"    {name}_ch = Channel.fromPath(params.{name})"
        )
    if not input_names:
        channel_lines.append("    input_ch = Channel.fromPath(params.input)")

    inputs_str = ", ".join(f"{name}_ch" for name in input_names) or "input_ch"

    # Build publishDir for outputs
    publish_lines = []
    for out_name in output_names:
        publish_lines.append(
            "    publishDir params.outdir, mode: 'copy'"
        )
        break  # One publishDir is enough for all outputs

    lines = [
        f"// Auto-generated wrapper for nf-core module {module_subdir}",
        f"// Entry ID: {entry_id}",
        "",
        f"include {{ {module_name} }} from './{include_path}'",
        "",
    ]

    if publish_lines:
        lines.extend(publish_lines)
        lines.append("")

    lines.extend([
        "workflow {",
        *channel_lines,
        f"    {module_name}({inputs_str})",
        "}",
        "",
    ])

    return "\n".join(lines)


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
