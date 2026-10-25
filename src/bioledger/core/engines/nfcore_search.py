"""nf-core modules search and download.

Searches the nf-core/modules GitHub repository for modules matching a query,
downloads module files to a local cache directory, and parses the downloaded
Nextflow DSL2 process into a minimal ExecutionSpec for the Nextflow adapter.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NFCORE_MODULES_REPO = "nf-core/modules"
NFCORE_GITHUB_API = "https://api.github.com/repos/nf-core/modules"
NFCORE_GITHUB_RAW = "https://raw.githubusercontent.com/nf-core/modules"
NFCORE_MODULES_REPO_URL = "https://github.com/nf-core/modules.git"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "nfcore"
_TREE_CACHE_TTL_SECONDS = 3600


@dataclass
class NFCoreResult:
    """A single nf-core module search hit."""

    name: str
    description: str
    subdirectory: str  # e.g. "align/bowtie2/align"


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_module_subdirs(ref: str = "master") -> list[str]:
    """Fetch all module subdirs via the git Trees API (single request).

    Real layout: ``modules/nf-core/<tool>/meta.yml`` (flat, e.g. fastqc)
    or ``modules/nf-core/<tool>/<subtool>/meta.yml`` (nested, e.g.
    bowtie2/align). Results are disk-cached for one hour.
    """
    import httpx

    cache_file = _CACHE_DIR / f"tree_{ref}.json"
    try:
        if (
            cache_file.exists()
            and time.time() - cache_file.stat().st_mtime < _TREE_CACHE_TTL_SECONDS
        ):
            cached = json.loads(cache_file.read_text())
            return cached.get("subdirs", [])
    except Exception:
        pass

    try:
        resp = httpx.get(
            f"{NFCORE_GITHUB_API}/git/trees/{ref}?recursive=1",
            timeout=60.0,
            headers=_github_headers(),
        )
        if resp.status_code != 200:
            logger.warning("nf-core tree fetch returned %d", resp.status_code)
            return []
        payload = resp.json()
        if not isinstance(payload, dict) or "tree" not in payload:
            return []
        subdirs = sorted(
            {
                m.group(1)
                for e in payload["tree"]
                if (
                    m := re.match(
                        r"modules/nf-core/(.+)/meta\.yml$", e.get("path", "")
                    )
                )
            }
        )
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"subdirs": subdirs}))
        except Exception:
            pass
        return subdirs
    except Exception as e:
        logger.warning("nf-core tree fetch failed: %s", e)
        return []


def _list_dir(url: str) -> list[dict]:
    """List a directory via the GitHub Contents API (fallback path)."""
    import httpx

    resp = httpx.get(url, timeout=30.0, headers=_github_headers())
    if resp.status_code != 200:
        return []
    payload = resp.json()
    return payload if isinstance(payload, list) else []


def _search_subdirs_via_contents() -> list[str]:
    """Contents-API fallback: detect modules by the presence of meta.yml
    at both the flat (tool/) and nested (tool/subtool/) levels."""
    base = f"{NFCORE_GITHUB_API}/contents/modules/nf-core"
    subdirs: list[str] = []
    for entry in _list_dir(base):
        if entry.get("type") != "dir":
            continue
        tool = entry["name"]
        children = _list_dir(f"{base}/{tool}")
        if any(c.get("name") == "meta.yml" for c in children):
            subdirs.append(tool)
        else:
            for c in children:
                if c.get("type") != "dir" or c["name"] == "test":
                    continue
                grandchildren = _list_dir(f"{base}/{tool}/{c['name']}")
                if any(g.get("name") == "meta.yml" for g in grandchildren):
                    subdirs.append(f"{tool}/{c['name']}")
    return sorted(subdirs)


def search_nfcore(query: str) -> list[NFCoreResult]:
    """Search nf-core/modules for modules matching the query.

    Fetches the module listing via the git Trees API (one request,
    disk-cached), falling back to a Contents-API walk when the tree
    payload is unavailable.
    """
    subdirs = _fetch_module_subdirs()
    if not subdirs:
        subdirs = _search_subdirs_via_contents()

    q = query.lower()
    return [
        NFCoreResult(
            name=s.split("/")[-1],
            description=_fetch_description(s),
            subdirectory=s,
        )
        for s in subdirs
        if q in s.lower()
    ]


def _fetch_description(subdir: str) -> str:
    """Fetch the description from the module's meta.yml."""
    import httpx

    url = f"{NFCORE_GITHUB_RAW}/master/modules/nf-core/{subdir}/meta.yml"
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code != 200:
            return ""
        try:
            import yaml

            meta = yaml.safe_load(resp.text) or {}
            desc = meta.get("description", "")
            return desc.strip() if isinstance(desc, str) else ""
        except Exception:
            # Line-scan fallback for malformed YAML
            for line in resp.text.splitlines():
                if line.strip().startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


def download_module(
    subdirectory: str,
    revision: str = "master",
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

    # Create minimal .nf-core.yml to avoid interactive prompts.
    # nf-core expects a pipeline layout — "modules" is not a valid
    # repository_type and makes install refuse to run.
    nfcore_yml = dest_dir / ".nf-core.yml"
    if not nfcore_yml.exists():
        nfcore_yml.write_text("repository_type: pipeline\n")

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

    # Read modules.json to get the resolved SHA.
    # Real layout: repos["https://github.com/nf-core/modules.git"]
    #   ["modules"]["nf-core"][<subdir>]["git_sha"]
    modules_json_path = dest_dir / "modules.json"
    resolved_sha = ""
    if modules_json_path.exists():
        try:
            modules_data = _json.loads(modules_json_path.read_text())
            repos = modules_data.get("repos", {})
            repo_entry = repos.get(NFCORE_MODULES_REPO_URL, {})
            resolved_sha = (
                repo_entry.get("modules", {})
                .get("nf-core", {})
                .get(subdirectory, {})
                .get("git_sha", "")
            )
            if not resolved_sha:
                # Fallback: walk nested dicts for {subdir: {"git_sha": ...}}
                # (covers simplified layouts and future schema drift)
                def _walk(obj: Any) -> str:
                    if isinstance(obj, dict):
                        entry = obj.get(subdirectory)
                        if isinstance(entry, dict) and entry.get("git_sha"):
                            return entry["git_sha"]
                        for v in obj.values():
                            found = _walk(v)
                            if found:
                                return found
                    return ""

                resolved_sha = _walk(repos)
        except Exception as e:
            logger.warning("Failed to read modules.json: %s", e)

    if not resolved_sha:
        # Last resort: resolve the ref to a commit SHA via the API so
        # provenance is still pinned when modules.json is absent/empty.
        try:
            import httpx

            resp = httpx.get(
                f"{NFCORE_GITHUB_API}/commits/{revision or 'master'}",
                timeout=15.0,
                headers=_github_headers(),
            )
            if resp.status_code == 200:
                resolved_sha = resp.json().get("sha", "")
        except Exception:
            pass

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
) -> tuple[str, str]:
    """Generate a wrapper main.nf and companion nextflow.config.

    The wrapper includes the module process and creates channels from
    params. ``publishDir`` is a process directive — it cannot appear at
    top level in a DSL2 script — so output publishing is configured in
    the companion nextflow.config via a ``withName`` selector that sets
    ``publishDir = params.outdir``, mirroring how real nf-core pipelines
    publish via ``conf/modules.config``. Outputs then land in the work
    directory given ``--outdir .``.

    Args:
        module_subdir: The nf-core subdirectory (e.g. "align/bowtie2/align").
        module_dir: Path to the installed module directory.
        meta: Parsed meta.yml dict (from ``parse_meta_yml``).
        entry_id: The ledger entry ID, used for the include path.

    Returns:
        Tuple of (wrapper main.nf content, nextflow.config content).
        The config string is empty when the module declares no outputs.
    """
    # Process name must match the ``process <NAME>`` declaration in the
    # module's main.nf (nf-core convention: UPPERCASE_SNAKE, e.g.
    # ``bowtie2/align`` → ``BOWTIE2_ALIGN``). parse_meta_yml extracts it;
    # fall back to the subdir-derived name when meta lacks it.
    process_name = (
        meta.get("process_name") or module_subdir.replace("/", "_").upper()
    )
    input_channels = meta.get("input_channels") or [
        {"name": n, "kind": "file", "has_meta": False}
        for n in meta.get("inputs", [])
    ]
    output_names = list(meta.get("outputs", {}).keys())

    # Build include directive — relative to ${projectDir}/<entry_id>/
    # The module files are in companion_files under the entry's namespace
    include_path = f"modules/nf-core/{module_subdir}/main"

    # Build channel declarations + positional call args. ``val`` channels
    # (e.g. BOWTIE2_ALIGN's save_unaligned/sort_bam booleans) are passed
    # as params.<name>. Their defaults must live in nextflow.config, NOT
    # the script — a ``params.x = ...`` assignment in the script would
    # silently override any ``--x`` flag on the CLI, while config params
    # are correctly overridden by CLI args.
    channel_lines: list[str] = []
    call_args: list[str] = []
    val_params: dict[str, Any] = {}
    for ch in input_channels:
        name = ch["name"]
        if ch.get("kind") == "val":
            val_params[name] = ch.get("default")
            call_args.append(f"params.{name}")
        elif ch.get("has_meta"):
            # tuple val(meta), path(<name>) — the meta map must carry
            # single_end so modules like BOWTIE2_ALIGN take the
            # single-file branch of `if (meta.single_end)`.
            channel_lines.append(
                f"    {name}_ch = Channel.of("
                f"[[id: 'sample', single_end: true], "
                f"file(params.{name}, checkIfExists: true)])"
            )
            call_args.append(f"{name}_ch")
        else:
            channel_lines.append(
                f"    {name}_ch = Channel.fromPath("
                f"params.{name}, checkIfExists: true)"
            )
            call_args.append(f"{name}_ch")
    if not call_args:
        channel_lines.append("    input_ch = Channel.fromPath(params.input)")
        call_args.append("input_ch")

    lines = [
        f"// Auto-generated wrapper for nf-core module {module_subdir}",
        f"// Entry ID: {entry_id}",
        "",
        f"include {{ {process_name} }} from './{include_path}'",
        "",
        "workflow {",
        *channel_lines,
        f"    {process_name}({', '.join(call_args)})",
        "}",
        "",
    ]

    config_lines: list[str] = [
        f"// Auto-generated config for nf-core module {module_subdir}",
    ]
    if val_params:
        config_lines.append("params {")
        for pname, pdefault in val_params.items():
            config_lines.append(f"    {pname} = {json.dumps(pdefault)}")
        config_lines.append("}")
    if output_names:
        config_lines += [
            "process {",
            f"    withName: '{process_name}' {{",
            "        publishDir = [path: params.outdir, mode: 'copy']",
            "    }",
            "}",
        ]
    config = "\n".join(config_lines) + "\n" if len(config_lines) > 1 else ""

    return "\n".join(lines), config


def _val_default(etype: str) -> Any:
    return {
        "boolean": False,
        "integer": 0,
        "float": 0.0,
        "string": "",
    }.get(etype, "")


def _strip_braces(pattern: str) -> str:
    """Expand bash brace alternation to the first alternative.

    ``"*_{fastqc.html}"`` → ``"*_fastqc.html"``;
    ``"*.{fa,fasta,fna}"`` → ``"*.fa"`` — a usable Python glob.
    """
    while "{" in pattern and "}" in pattern:
        start = pattern.index("{")
        end = pattern.index("}", start)
        alts = pattern[start + 1 : end].split(",")
        pattern = pattern[:start] + alts[0] + pattern[end + 1 :]
    return pattern


def parse_meta_yml(module_dir: Path) -> dict:
    """Parse a downloaded nf-core module's meta.yml for input/output metadata.

    Returns a dict with keys:
    - ``inputs``: list of file input names (channel wiring order)
    - ``outputs``: dict of {emit_name: glob pattern} (for output discovery)
    - ``container``: docker image string (may be empty)
    - ``process_name``: ``process <NAME>`` from main.nf (may be empty)
    - ``input_channels``: positional channel descriptors —
      ``{"name", "kind": "file"|"val", "has_meta", "type", "default"}``
    - ``parameters``: {val_name: default} for non-file channels

    Reads the structured ``meta.yml`` rather than line-scanning — the real
    schema nests channels as ``- - meta: {type: map} - reads: {type: file}``
    which the old regex approach could not parse at all.
    """
    meta_path = module_dir / "meta.yml"
    inputs: list[str] = []
    outputs: dict[str, str] = {}
    container = ""
    process_name = ""
    input_channels: list[dict] = []
    parameters: dict[str, Any] = {}

    meta: dict = {}
    if meta_path.exists():
        try:
            import yaml

            meta = yaml.safe_load(meta_path.read_text()) or {}
        except Exception as e:
            logger.warning("Failed to parse %s: %s", meta_path, e)
    else:
        logger.warning("meta.yml not found in %s", module_dir)

    # --- inputs ---
    # Each channel is either a list of single-key dicts (tuple channel,
    # e.g. [meta, reads]) or a single-key dict (val channel).
    for channel in meta.get("input") or []:
        elements = channel if isinstance(channel, list) else [channel]
        file_name = ""
        has_meta = False
        val_name = ""
        val_type = ""
        for elem in elements:
            if not isinstance(elem, dict) or not elem:
                continue
            if "name" in elem and len(elem) > 1:
                # Legacy flat shape: {name: x, type: file, ...}
                name, info = elem["name"], elem
            else:
                name, info = next(iter(elem.items()))
                info = info if isinstance(info, dict) else {}
            etype = info.get("type", "")
            if etype == "map":
                has_meta = True
            elif etype in ("file", "directory", "path"):
                file_name = name
            elif etype in ("boolean", "integer", "float", "string"):
                val_name, val_type = name, etype
        if file_name:
            inputs.append(file_name)
            input_channels.append(
                {"name": file_name, "kind": "file", "has_meta": has_meta}
            )
        elif val_name:
            default = _val_default(val_type)
            parameters[val_name] = default
            input_channels.append(
                {
                    "name": val_name,
                    "kind": "val",
                    "has_meta": False,
                    "type": val_type,
                    "default": default,
                }
            )

    # --- outputs ---
    # meta["output"] is a dict keyed by emit name ({bam: [[meta, "*.bam"]]})
    # or, in some modules, a list of single-key dicts. The file element's
    # dict key is the Nextflow emit glob; its ``pattern:`` field may carry
    # bash braces ("*_{fastqc.html}") that Path.rglob cannot match.
    raw_outputs = meta.get("output") or {}
    emit_items: list[tuple[str, Any]] = []
    if isinstance(raw_outputs, dict):
        emit_items = list(raw_outputs.items())
    else:
        for entry in raw_outputs:
            if not isinstance(entry, dict):
                continue
            if "name" in entry and ("type" in entry or "pattern" in entry):
                # Legacy flat shape: {name: x, type: file, pattern: "*.bam"}
                emit_items.append((entry["name"], entry.get("pattern", "")))
            else:
                emit_items.extend(entry.items())
    for emit_name, channels in emit_items:
        if emit_name.startswith("versions"):
            continue
        if isinstance(channels, str):
            outputs[emit_name] = channels
            continue
        for channel in channels or []:
            elements = channel if isinstance(channel, list) else [channel]
            for elem in elements:
                if not isinstance(elem, dict) or not elem:
                    continue
                key, info = next(iter(elem.items()))
                info = info or {}
                if info.get("type") not in ("file", "directory"):
                    continue
                if isinstance(key, str) and "{" not in key:
                    outputs[emit_name] = key
                else:
                    pattern = info.get("pattern") or str(key)
                    outputs[emit_name] = _strip_braces(pattern)
                break

    # --- container ---
    # Prefer structured meta.yml: containers.docker["linux/amd64"].name
    containers = meta.get("containers") or {}
    docker = containers.get("docker") or {}
    amd64 = docker.get("linux/amd64") or {}
    if isinstance(amd64, dict):
        container = amd64.get("name") or ""
    elif isinstance(amd64, str):
        container = amd64

    nf_path = module_dir / "main.nf"
    nf_content = nf_path.read_text() if nf_path.exists() else ""

    if not container and nf_content:
        # Fallback: main.nf container directive — but real nf-core modules
        # use a workflow.containerEngine ternary whose ${...} expression a
        # bare regex captures verbatim; only accept static images and
        # prefer depot.galaxyproject.org (the conda-built default).
        candidates = re.findall(r"container\s+['\"]([^'\"]+)['\"]", nf_content)
        for candidate in candidates:
            if "${" in candidate:
                continue
            if "depot.galaxyproject.org" in candidate:
                container = candidate
                break
            if not container:
                container = candidate

    # --- process name ---
    if nf_content:
        m = re.search(r"process\s+([A-Za-z0-9_]+)", nf_content)
        if m:
            process_name = m.group(1)
    if not process_name:
        name = meta.get("name")
        process_name = name.upper() if isinstance(name, str) else ""

    return {
        "inputs": inputs,
        "outputs": outputs,
        "container": container,
        "process_name": process_name,
        "input_channels": input_channels,
        "parameters": parameters,
    }
