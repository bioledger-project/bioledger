"""Snakemake wrapper search and discovery.

Searches the snakemake/snakemake-wrappers GitHub repository for wrappers
matching a query, parses the wrapper's test/Snakefile and meta.yaml into
a minimal ExecutionSpec, and generates a Snakefile that uses the native
``wrapper:`` directive so Snakemake downloads and runs the wrapper.py
automatically.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import textwrap
import time
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

WRAPPERS_REPO = "snakemake/snakemake-wrappers"
WRAPPERS_GITHUB_API = "https://api.github.com/repos/snakemake/snakemake-wrappers"
WRAPPERS_GITHUB_RAW = "https://raw.githubusercontent.com/snakemake/snakemake-wrappers"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "snakemake_wrappers"
_TREE_CACHE_TTL_SECONDS = 3600
# Segments that never contain wrappers (repo infrastructure)
_NON_WRAPPER_TOP_DIRS = {
    ".github", "ci", "docs", "images", "misc", "profile",
    "test", "test-data", "utils-r",
}


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class SnakemakeWrapperResult:
    """A single Snakemake wrapper search hit."""

    name: str
    description: str
    path: str  # e.g. "bio/samtools/sort"


def _fetch_wrapper_paths(ref: str = "master") -> list[str]:
    """Fetch wrapper dirs via the git Trees API (single request, cached).

    A directory is a wrapper iff it contains ``wrapper.py`` — the old
    Contents-API walk treated *any* subdirectory of a tool dir as a
    wrapper, which emitted non-wrapper paths like ``bio/bgzip/test``.
    """
    import httpx

    cache_file = _CACHE_DIR / f"tree_{ref}.json"
    try:
        if (
            cache_file.exists()
            and time.time() - cache_file.stat().st_mtime < _TREE_CACHE_TTL_SECONDS
        ):
            return json.loads(cache_file.read_text()).get("paths", [])
    except Exception:
        pass

    try:
        resp = httpx.get(
            f"{WRAPPERS_GITHUB_API}/git/trees/{ref}?recursive=1",
            timeout=60.0,
            headers=_github_headers(),
        )
        if resp.status_code != 200:
            logger.warning("wrappers tree fetch returned %d", resp.status_code)
            return []
        payload = resp.json()
        if not isinstance(payload, dict) or "tree" not in payload:
            return []
        paths = sorted(
            {
                m.group(1)
                for e in payload["tree"]
                if (m := re.match(r"(.+)/wrapper\.py$", e.get("path", "")))
                and m.group(1).split("/")[0] not in _NON_WRAPPER_TOP_DIRS
            }
        )
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"paths": paths}))
        except Exception:
            pass
        return paths
    except Exception as e:
        logger.warning("wrappers tree fetch failed: %s", e)
        return []


def search_wrappers(query: str) -> list[SnakemakeWrapperResult]:
    """Search the Snakemake wrappers repo for wrappers matching *query*."""
    q_lower = query.lower()
    results: list[SnakemakeWrapperResult] = []
    for path in _fetch_wrapper_paths():
        if q_lower in path.lower():
            results.append(
                SnakemakeWrapperResult(
                    name=path.split("/")[-1],
                    description=path,
                    path=path,
                )
            )
    return results


def _resolve_ref_sha(ref: str) -> str:
    """Resolve a tag/branch/SHA to a commit SHA via the GitHub API."""
    import httpx

    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    resp = httpx.get(
        f"{WRAPPERS_GITHUB_API}/commits/{ref}",
        timeout=30.0,
        headers=_github_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("sha", ref)


def download_wrapper(
    wrapper_path: str,
    revision: str | None = None,
    dest_dir: Path | None = None,
) -> tuple[Path, str]:
    """Download a Snakemake wrapper's meta.yaml, environment.yaml,
    wrapper.py and test/Snakefile.

    ``revision`` may be a tag, branch, or commit SHA — it is resolved to
    a SHA for the returned provenance value. Raises ``RuntimeError`` when
    the required ``test/Snakefile`` cannot be fetched (404s used to be
    silently swallowed, producing unparsable empty dirs).

    Returns tuple of (path to downloaded wrapper dir, resolved SHA).
    """
    import httpx

    resolved = _resolve_ref_sha(revision or "master")
    dest = dest_dir or (_CACHE_DIR / wrapper_path.replace("/", "_"))
    dest.mkdir(parents=True, exist_ok=True)

    # (source path, dest name, required)
    files_to_download = [
        ("meta.yaml", "meta.yaml", False),
        ("environment.yaml", "environment.yaml", False),
        ("wrapper.py", "wrapper.py", False),
        ("test/Snakefile", "test_Snakefile", True),
    ]

    for src_name, dest_name, required in files_to_download:
        url = f"{WRAPPERS_GITHUB_RAW}/{resolved}/{wrapper_path}/{src_name}"
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code == 200:
                (dest / dest_name).write_text(resp.text)
            elif required:
                raise RuntimeError(
                    f"{wrapper_path}/{src_name}@{resolved}: "
                    f"HTTP {resp.status_code}"
                )
            else:
                logger.warning(
                    "%s returned %d", src_name, resp.status_code,
                )
        except RuntimeError:
            raise
        except Exception as e:
            if required:
                raise RuntimeError(
                    f"Failed to download {wrapper_path}/{src_name}: {e}"
                )
            logger.warning("Failed to download %s: %s", src_name, e)

    return dest, resolved


def parse_wrapper_to_spec(
    wrapper_dir: Path,
    wrapper_path: str,
) -> ExecutionSpec:
    """Parse a downloaded Snakemake wrapper into a minimal ExecutionSpec.

    Reads the ``test/Snakefile`` to extract the rule interface (input, output,
    params, threads) and ``meta.yaml`` for the description.
    """
    # Parse meta.yaml for description
    description = ""
    meta_path = wrapper_dir / "meta.yaml"
    if meta_path.exists():
        try:
            import yaml

            meta = yaml.safe_load(meta_path.read_text())
            description = meta.get("description", "")
        except Exception:
            pass

    # Parse test/Snakefile for the rule interface
    snakefile_path = wrapper_dir / "test_Snakefile"
    inputs: dict[str, ToolInput] = {}
    outputs: dict[str, ToolOutput] = {}
    parameters: dict[str, ToolParameter] = {}

    if snakefile_path.exists():
        content = snakefile_path.read_text()
        inputs = _parse_snakefile_inputs(content)
        outputs = _parse_snakefile_outputs(content)
        parameters = _parse_snakefile_params(content)

    # Derive a name from the wrapper path
    name = wrapper_path.replace("/", "_")

    return ExecutionSpec(
        name=name,
        version="",
        description=description,
        container="",
        executors=["snakemake"],
        command="",
        inputs=inputs,
        outputs=outputs,
        parameters=parameters,
    )


def _find_wrapper_rule(content: str) -> str | None:
    """Body of the first ``rule x:`` block containing a ``wrapper:`` directive.

    test/Snakefiles frequently contain several rules plus
    ``use rule ... with:`` variants — only real ``rule`` blocks are
    scanned and the one that actually invokes the wrapper is selected.
    """
    bodies = [
        m.group(1)
        for m in re.finditer(
            r"(?ms)^rule\s+\w+\s*:[^\n]*\n(.*?)(?=^rule\s|^use\s+rule\s|\Z)",
            content,
        )
    ]
    for body in bodies:
        if re.search(r"(?m)^\s*wrapper\s*:", body):
            return body
    # No wrapper: directive anywhere — fall back to the first rule
    return bodies[0] if bodies else None


def _extract_section(rule_body: str, keyword: str) -> str | None:
    """Extract an indented ``keyword:`` section from a rule body.

    Sections end at the next line at or below the section's own indent —
    handles multi-line call expressions (``multiext(\\n ...``) that a
    line-by-line regex cannot parse.
    """
    lines = rule_body.split("\n")
    start = None
    collected: list[str] = []
    base_indent = 0
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{keyword}\s*:", line):
            start = i
            base_indent = len(line) - len(line.lstrip())
            after = line.split(":", 1)[1].strip()
            if after:
                collected.append(after)
            break
    if start is None:
        return None
    for line in lines[start + 1 :]:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped and indent <= base_indent:
            break
        collected.append(line)
    return "\n".join(collected)


def _parse_block_call(block: str) -> tuple[list[ast.expr], dict[str, ast.expr]]:
    """Parse a section body as a call's positional + keyword args.

    Wrapping in ``_b(...)`` turns real Snakemake syntax — ``name=value``
    keywords, positional strings, comments, multi-line calls — into
    plain Python ``ast`` can handle.
    """
    try:
        tree = ast.parse(f"_b(\n{textwrap.dedent(block)}\n)")
    except SyntaxError:
        return [], {}
    call = tree.body[0].value
    return list(call.args), {
        k.arg: k.value for k in call.keywords if k.arg
    }


def _literal_strings(node: ast.expr) -> list[str]:
    """Flatten string literals out of a value node (str/List/Tuple/Call)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [s for el in node.elts for s in _literal_strings(el)]
    if isinstance(node, ast.Call):
        return [s for a in node.args for s in _literal_strings(a)]
    return []


def _const_value(node: ast.expr):
    return node.value if isinstance(node, ast.Constant) else None


def _param_type_for(value) -> ParamType:
    if isinstance(value, bool):
        return ParamType.BOOLEAN
    if isinstance(value, int):
        return ParamType.INTEGER
    if isinstance(value, float):
        return ParamType.FLOAT
    return ParamType.STRING


def _parse_snakefile_inputs(content: str) -> dict[str, ToolInput]:
    """Extract input names from the wrapper rule's ``input:`` section."""
    rule = _find_wrapper_rule(content)
    block = _extract_section(rule, "input") if rule else None
    inputs: dict[str, ToolInput] = {}
    if not block:
        return inputs
    pos, kw = _parse_block_call(block)
    for name, node in kw.items():
        inputs[name] = ToolInput(
            name=name,
            collection=isinstance(node, (ast.List, ast.Tuple)),
            format="any",
        )
    for i, node in enumerate(pos):
        name = f"input_{i}"
        inputs[name] = ToolInput(
            name=name,
            collection=isinstance(node, (ast.List, ast.Tuple)),
            format="any",
        )
    return inputs


def _concrete_pattern(pattern: str) -> str:
    """Replace ``{wildcard}`` placeholders with a concrete token.

    Snakemake wildcards (``{sample}``) are unresolvable in a standalone
    generated Snakefile (nothing fills them), and are useless as Python
    glob patterns for output discovery — substitute a fixed ``out``.
    """
    return re.sub(r"\{[^}]*\}", "out", pattern)


def _parse_snakefile_outputs(content: str) -> dict[str, ToolOutput]:
    """Extract output names and patterns from the wrapper rule."""
    rule = _find_wrapper_rule(content)
    block = _extract_section(rule, "output") if rule else None
    outputs: dict[str, ToolOutput] = {}
    if not block:
        return outputs
    pos, kw = _parse_block_call(block)
    for name, node in kw.items():
        lits = _literal_strings(node)
        pattern = _concrete_pattern(lits[0]) if lits else ""
        outputs[name] = ToolOutput(
            name=name, pattern=pattern, format="any",
        )
    for i, node in enumerate(pos):
        lits = _literal_strings(node)
        name = f"output_{i}"
        pattern = _concrete_pattern(lits[0]) if lits else ""
        outputs[name] = ToolOutput(
            name=name, pattern=pattern, format="any",
        )
    return outputs


def _parse_snakefile_params(content: str) -> dict[str, ToolParameter]:
    """Extract params + the threads directive from the wrapper rule.

    ``threads`` is surfaced as an INTEGER parameter (default = the
    rule's declared value, minimum 2 — wrappers like bowtie2/align
    raise ``ValueError`` when ``snakemake.threads < 2``). The generator
    maps it to the ``threads:`` directive rather than ``params:``.
    """
    rule = _find_wrapper_rule(content)
    block = _extract_section(rule, "params") if rule else None
    parameters: dict[str, ToolParameter] = {}
    if block:
        _pos, kw = _parse_block_call(block)
        for name, node in kw.items():
            default = _const_value(node)
            parameters[name] = ToolParameter(
                name=name,
                type=_param_type_for(default),
                default=default,
                description="",
            )
    if rule:
        m = re.search(r"(?m)^\s*threads\s*:\s*(\d+)", rule)
        threads = max(2, int(m.group(1))) if m else 2
        parameters["threads"] = ToolParameter(
            name="threads",
            type=ParamType.INTEGER,
            default=threads,
            description="Minimum threads for wrapper.py (>= 2 required by some wrappers).",
        )
    return parameters


def generate_wrapper_snakefile(
    wrapper_path: str,
    spec: ExecutionSpec,
    revision: str = "master",
) -> str:
    """Generate a Snakefile that uses the native ``wrapper:`` directive.

    The generated Snakefile has a single rule that wires inputs/outputs/params
    from the spec to the Snakemake wrapper. The ``wrapper:`` directive tells
    Snakemake to download and execute ``wrapper.py`` automatically.
    """
    rule_name = spec.name.replace("/", "_")
    lines: list[str] = []

    # ``threads`` is a rule directive, not a params entry — wrappers read
    # snakemake.threads (bowtie2/align requires >= 2). Parsed spec carries
    # it as an INTEGER parameter so users can override via --config.
    threads_param = spec.parameters.get("threads")
    try:
        threads_default = int(threads_param.default) if threads_param else 2
    except (TypeError, ValueError):
        threads_default = 2
    threads_default = max(2, threads_default)
    run_params = {
        k: v for k, v in spec.parameters.items() if k != "threads"
    }

    lines.append(f'rule {rule_name}:')
    lines.append('    input:')
    if spec.inputs:
        input_items = []
        for name, inp in spec.inputs.items():
            if inp.collection:
                # List-valued inputs must stay lists — a bare string
                # gives wrapper.py len("path") == N instead of 1..2.
                input_items.append(
                    f"        {name}=config.get('{name}', [])"
                )
            else:
                input_items.append(f"        {name}=config['{name}']")
        lines.append(",\n".join(input_items))
    else:
        lines.append('        []')

    lines.append('    output:')
    if spec.outputs:
        output_items = []
        for name, out in spec.outputs.items():
            output_items.append(f'        {name}="{out.pattern}"')
        lines.append(",\n".join(output_items))
    else:
        lines.append('        []')

    if run_params:
        lines.append('    params:')
        param_items = []
        for name, param in run_params.items():
            default_repr = repr(param.default) if param.default is not None else "''"
            param_items.append(
                f"        {name}=config.get('{name}', {default_repr})"
            )
        lines.append(",\n".join(param_items))

    lines.append(
        f"    threads: max(2, config.get('threads', {threads_default}))"
    )
    lines.append('    wrapper:')
    lines.append(f'        "{revision}/{wrapper_path}"')

    return "\n".join(lines) + "\n"
