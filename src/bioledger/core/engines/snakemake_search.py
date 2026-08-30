"""Snakemake wrapper search and discovery.

Searches the snakemake/snakemake-wrappers GitHub repository for wrappers
matching a query, parses the wrapper's test/Snakefile and meta.yaml into
a minimal ExecutionSpec, and generates a Snakefile that uses the native
``wrapper:`` directive so Snakemake downloads and runs the wrapper.py
automatically.
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
    ToolParameter,
)

logger = logging.getLogger(__name__)

WRAPPERS_REPO = "snakemake/snakemake-wrappers"
WRAPPERS_GITHUB_API = "https://api.github.com/repos/snakemake/snakemake-wrappers"
WRAPPERS_GITHUB_RAW = "https://raw.githubusercontent.com/snakemake/snakemake-wrappers"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "snakemake_wrappers"


@dataclass
class SnakemakeWrapperResult:
    """A single Snakemake wrapper search hit."""

    name: str
    description: str
    path: str  # e.g. "bio/samtools/sort"


def search_wrappers(query: str) -> list[SnakemakeWrapperResult]:
    """Search the Snakemake wrappers repo for wrappers matching *query*.

    Uses the GitHub API to list directories under the repo root, then
    filters by the query string.
    """
    import httpx

    results: list[SnakemakeWrapperResult] = []

    # GitHub API: list contents at top-level dirs (bio/, utils/, etc.)
    try:
        resp = httpx.get(
            f"{WRAPPERS_GITHUB_API}/contents",
            timeout=30.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        top_dirs = [d["name"] for d in resp.json() if d["type"] == "dir"]
    except Exception as e:
        logger.warning("Failed to list wrapper categories: %s", e)
        return results

    q_lower = query.lower()

    for category in top_dirs:
        try:
            resp = httpx.get(
                f"{WRAPPERS_GITHUB_API}/contents/{category}",
                timeout=30.0,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            tool_dirs = [d for d in resp.json() if d["type"] == "dir"]
        except Exception:
            continue

        for tool_dir in tool_dirs:
            tool_name = tool_dir["name"]
            # Check subdirs (e.g. bio/samtools/sort)
            try:
                resp = httpx.get(
                    f"{WRAPPERS_GITHUB_API}/contents/{category}/{tool_name}",
                    timeout=30.0,
                    headers={"Accept": "application/vnd.github+json"},
                )
                resp.raise_for_status()
                subdirs = [d for d in resp.json() if d["type"] == "dir"]
            except Exception:
                subdirs = []

            # If there are subdirs, each is a wrapper (e.g. samtools/sort)
            if subdirs:
                for sd in subdirs:
                    sd_name = sd["name"]
                    full_path = f"{category}/{tool_name}/{sd_name}"
                    if q_lower in full_path.lower():
                        results.append(
                            SnakemakeWrapperResult(
                                name=sd_name,
                                description=f"{tool_name} {sd_name}",
                                path=full_path,
                            )
                        )
            else:
                # The tool dir itself is the wrapper
                full_path = f"{category}/{tool_name}"
                if q_lower in full_path.lower():
                    results.append(
                        SnakemakeWrapperResult(
                            name=tool_name,
                            description=tool_name,
                            path=full_path,
                        )
                    )

    return results


def download_wrapper(
    wrapper_path: str,
    revision: str | None = None,
    dest_dir: Path | None = None,
) -> tuple[Path, str]:
    """Download a Snakemake wrapper's meta.yaml and test/Snakefile.

    Returns tuple of (path to downloaded wrapper dir, resolved revision).
    """
    import httpx

    ref = revision or "master"
    dest = dest_dir or (_CACHE_DIR / wrapper_path.replace("/", "_"))
    dest.mkdir(parents=True, exist_ok=True)

    files_to_download = [
        ("meta.yaml", "meta.yaml"),
        ("environment.yaml", "environment.yaml"),
        ("test/Snakefile", "test_Snakefile"),
    ]

    for src_name, dest_name in files_to_download:
        url = f"{WRAPPERS_GITHUB_RAW}/{ref}/{wrapper_path}/{src_name}"
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code == 200:
                (dest / dest_name).write_text(resp.text)
        except Exception as e:
            logger.warning("Failed to download %s: %s", src_name, e)

    # Get the resolved revision if not pinned
    if revision:
        resolved = revision
    else:
        try:
            resp = httpx.get(
                f"{WRAPPERS_GITHUB_API}/commits/master",
                timeout=30.0,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            resolved = resp.json().get("sha", "master")
        except Exception:
            resolved = "master"

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


def _strip_inline_comment(line: str) -> str:
    """Strip a trailing ``# comment`` from a line (lightweight, not string-aware)."""
    idx = line.find("#")
    if idx == -1:
        return line
    return line[:idx]


def _parse_snakefile_inputs(content: str) -> dict[str, ToolInput]:
    """Extract input names from a test/Snakefile rule."""
    inputs: dict[str, ToolInput] = {}
    # Match input: block — could be single line or multi-line
    input_block = _extract_block(content, "input")
    if input_block:
        for raw_line in input_block.split("\n"):
            line = _strip_inline_comment(raw_line).strip().rstrip(",")
            if not line or line.startswith("#"):
                continue
            # Patterns: "name": "path", "name": ["a", "b"], or just "path"
            m = re.match(r'"?(\w+)"?\s*:\s*(.+)', line)
            if m:
                name = m.group(1)
                inputs[name] = ToolInput(name=name, collection=False, format="any")
            elif line.startswith('"') or line.startswith("'"):
                # Unnamed input — use index
                name = f"input_{len(inputs)}"
                inputs[name] = ToolInput(name=name, collection=False, format="any")
    return inputs


def _parse_snakefile_outputs(content: str) -> dict[str, ToolOutput]:
    """Extract output names and patterns from a test/Snakefile rule."""
    outputs: dict[str, ToolOutput] = {}
    output_block = _extract_block(content, "output")
    if output_block:
        for raw_line in output_block.split("\n"):
            line = _strip_inline_comment(raw_line).strip().rstrip(",")
            if not line or line.startswith("#"):
                continue
            m = re.match(r'"?(\w+)"?\s*:\s*"(.+)"', line)
            if m:
                name = m.group(1)
                pattern = m.group(2)
                outputs[name] = ToolOutput(name=name, pattern=pattern, format="any")
            elif line.startswith('"') or line.startswith("'"):
                pattern = line.strip("\"'")
                name = f"output_{len(outputs)}"
                outputs[name] = ToolOutput(name=name, pattern=pattern, format="any")
    return outputs


def _parse_snakefile_params(content: str) -> dict[str, ToolParameter]:
    """Extract params from a test/Snakefile rule."""
    parameters: dict[str, ToolParameter] = {}
    params_block = _extract_block(content, "params")
    if params_block:
        for raw_line in params_block.split("\n"):
            line = _strip_inline_comment(raw_line).strip().rstrip(",")
            if not line or line.startswith("#"):
                continue
            m = re.match(r'(\w+)\s*=\s*(.+)', line)
            if m:
                name = m.group(1)
                value = m.group(2).strip()
                # Prefer extracting a properly quoted string value; fall back
                # to the raw token (e.g. numeric literals) otherwise.
                quoted = re.match(r'^["\'](.*)["\']$', value)
                default = quoted.group(1) if quoted else value
                parameters[name] = ToolParameter(
                    name=name,
                    type=ParamType.STRING,
                    default=default,
                    description="",
                )
    return parameters


def _extract_block(content: str, keyword: str) -> str | None:
    """Extract the content of a rule block (input:, output:, params:).

    Handles both single-line and multi-line formats.
    """
    # Match "keyword:" followed by either inline content or indented block
    pattern = rf'{keyword}\s*:\s*\n?(.*?)(?=\n\s*\w+\s*:|\n\s*wrapper|\n\s*threads|\Z)'
    m = re.search(pattern, content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


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

    lines.append(f'rule {rule_name}:')
    lines.append('    input:')
    if spec.inputs:
        input_items = []
        for name in spec.inputs:
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

    if spec.parameters:
        lines.append('    params:')
        param_items = []
        for name, param in spec.parameters.items():
            default_repr = repr(param.default) if param.default is not None else "''"
            param_items.append(
                f"        {name}=config.get('{name}', {default_repr})"
            )
        lines.append(",\n".join(param_items))

    lines.append('    threads: 1')
    lines.append('    wrapper:')
    lines.append(f'        "{revision}/{wrapper_path}"')

    return "\n".join(lines) + "\n"
