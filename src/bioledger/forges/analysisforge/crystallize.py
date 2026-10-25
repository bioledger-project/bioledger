from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession

logger = logging.getLogger(__name__)


def _produced_outputs(entry: LedgerEntry) -> list[tuple[int, str]]:
    """(emit_index, basename) pairs for an entry's genuine outputs.

    Files that appear as both input and output with the SAME sha256 are
    excluded — they are staged inputs that leaked into the output list
    (the tool didn't produce them, they were just staged there). If the
    sha256 DIFFERS (e.g. bgzip re-compresses a .fna.gz to different
    content), the output is legitimate and downstream tools chain to it.
    Emit indices match ``_io_declarations``' ``out{idx}`` naming, which
    enumerates the unfiltered recorded output list.
    """
    input_sha: dict[str, str] = {}
    for f in entry.files:
        if f.role == "input":
            input_sha[Path(f.path).name] = f.sha256
    produced: list[tuple[int, str]] = []
    for idx, filename in enumerate(_output_filenames(entry)):
        if filename in input_sha:
            output_sha = next(
                (f.sha256 for f in entry.files
                 if f.role == "output" and Path(f.path).name == filename),
                "",
            )
            if output_sha == input_sha[filename]:
                continue
        produced.append((idx, filename))
    return produced


def _update_source_map(
    source_map: dict[str, tuple[str, str]],
    entry: LedgerEntry,
) -> None:
    """Record an entry's produced files; later producers overwrite."""
    for idx, filename in _produced_outputs(entry):
        source_map[filename] = (entry.id, f"out{idx}")


def _build_dependency_graph(
    entries: list[LedgerEntry],
) -> dict[str, set[str]]:
    """Build a dependency graph based on file-level input/output matching.

    Entry A depends on entry B if A consumes a file that B produces.
    The source map is built incrementally so each input resolves against
    the most recent producer *before* the consumer — an intermediate
    step that rewrites a file under the same basename becomes the
    dependency, not the original producer.
    """
    deps: dict[str, set[str]] = {e.id: set() for e in entries}
    source_map: dict[str, tuple[str, str]] = {}
    for entry in entries:
        for filename in _input_name_to_filename(entry).values():
            producer = source_map.get(filename)
            if producer is not None and producer[0] != entry.id:
                deps[entry.id].add(producer[0])
        _update_source_map(source_map, entry)
    return deps


def _topological_order_from_deps(
    entries: list[LedgerEntry],
    deps: dict[str, set[str]],
) -> list[LedgerEntry]:
    """Topological sort respecting file-level dependencies."""
    entry_by_id = {e.id: e for e in entries}
    visited: set[str] = set()
    order: list[LedgerEntry] = []

    def dfs(eid: str) -> None:
        if eid in visited:
            return
        visited.add(eid)
        for dep_id in deps.get(eid, set()):
            dfs(dep_id)
        order.append(entry_by_id[eid])

    for entry in entries:
        dfs(entry.id)

    return order


def _proc_name(entry: LedgerEntry, index: int) -> str:
    """Generate a Nextflow process name from a ledger entry."""
    base = entry.tool_spec_name or (
        entry.container.image.split("/")[-1].split(":")[0] if entry.container else "unknown"
    )
    return re.sub(r"[^A-Za-z0-9_]", "_", f"step_{index}_{base}")


def _input_name_to_filename(entry: LedgerEntry) -> dict[str, str]:
    """Extract mapping of tool input name -> staged filename from the entry.

    Uses ContainerInfo.input_mapping which records {slot: staged_basename}
    from the per-run staging process. This maps directly to Nextflow's
    `path <name>` declarations.
    """
    mapping: dict[str, str] = {}
    if not entry.container:
        return mapping

    # input_mapping records {slot: staged_basename} from per-run staging
    if entry.container.input_mapping:
        for name, staged_basename in entry.container.input_mapping.items():
            mapping[name] = staged_basename
    return mapping


def _output_filenames(entry: LedgerEntry) -> list[str]:
    """Return output filenames captured during the run."""
    return [Path(f.path).name for f in entry.files if f.role == "output"]


def _output_name_to_filename(entry: LedgerEntry) -> dict[str, str]:
    """Map spec output name -> concrete filename for script rendering.

    The process ``output:`` block declares ``path '<file>', emit: <name>``;
    a template that writes ``{{ outputs.<name>.path }}`` must produce that
    same filename. Literal patterns are used directly; glob patterns are
    resolved against the recorded run's actual output filenames.
    """
    import fnmatch

    spec = entry.tool_spec_snapshot or {}
    recorded = _output_filenames(entry)
    result: dict[str, str] = {}
    for name, odef in (spec.get("outputs") or {}).items():
        pattern = (
            odef.get("pattern", "")
            if isinstance(odef, dict) else getattr(odef, "pattern", "")
        ) or ""
        if pattern and "*" not in pattern:
            result[name] = pattern
        else:
            # No recorded output (running/failed run): a bare `name` may
            # not satisfy the declared `path '<glob>', emit:` declaration —
            # substitute `*` so the fallback still matches the glob.
            fallback = pattern.replace("*", name) if "*" in pattern else name
            result[name] = next(
                (f for f in recorded if fnmatch.fnmatch(f, pattern or "*")),
                fallback,
            )
    return result


def _render_script_for_nextflow(entry: LedgerEntry, input_names: list[str]) -> str:
    """Re-render the tool spec command template with Nextflow-style variables.

    In Nextflow, declared `path <name>` inputs are staged into the work dir with
    their original filename and are referenced as the bash variable ``${name}``.
    Output files in the work dir (``.``) are captured by the output declaration.
    """
    from bioledger.core.engines._subprocess import (
        _NextflowVar,
        _OutputRef,
        nextflow_jinja_env,
    )

    spec = entry.tool_spec_snapshot or {}
    template_str = spec.get("command")
    if not template_str:
        if entry.container and entry.container.command:
            return " ".join(entry.container.command)
        return "echo 'no command'"
    # Jinja context: inputs use Nextflow shell variables; output dir is '.'
    # Historical ledger entries may have stored parameters as a list;
    # current entries store a dict. Handle both.
    params_data = spec.get("parameters") or {}
    if isinstance(params_data, list):
        params_dict = {p.get("name"): p.get("default") for p in params_data}
    else:
        params_dict = {k: v.get("default") for k, v in params_data.items()}
    # Overlay entry.params (runtime overrides) on top of spec defaults
    if entry.params:
        params_dict = {**params_dict, **entry.params}
    # outputs.<name>/.path -> the filename declared in the output: block —
    # without this, `{{ outputs.x.path }}` silently renders "" and `-o `
    # swallows the next argument.
    outputs_ctx: dict[str, Any] = {"_dir": "."}
    for name, fname in _output_name_to_filename(entry).items():
        outputs_ctx[name] = _OutputRef(fname)
    # Declared inputs become ${name} path vars; spec inputs that weren't
    # mapped at run time (optional inputs the caller omitted) bind to ""
    # so `{% if inputs.opt %}` and bare `{{ inputs.opt }}` match live
    # execution instead of emitting an undeclared path variable.
    inputs_ctx: dict[str, Any] = {
        name: _NextflowVar(f"${{{name}}}") for name in input_names
    }
    for name in (spec.get("inputs") or {}):
        inputs_ctx.setdefault(name, "")
    context = {
        "inputs": inputs_ctx,
        "outputs": outputs_ctx,
        "parameters": params_dict,
    }
    try:
        return nextflow_jinja_env().from_string(template_str).render(context)
    except Exception:
        logger.warning("Failed to render Nextflow script for %s", entry.id)
        if entry.container and entry.container.command:
            return " ".join(entry.container.command)
        return "echo 'render failed'"


def _declared_input_names(entry: LedgerEntry) -> list[str]:
    """Names of the Nextflow ``path`` inputs to declare for an entry.

    Prefers the slots actually staged at run time (``input_mapping``),
    ordered by spec declaration first so declarations and workflow-call
    channels agree; extra staged slots not in the spec (e.g. expanded
    collection slots ``reads__0``) follow. Falls back to spec names for
    legacy entries with no recorded mapping.
    """
    spec_inputs = (entry.tool_spec_snapshot or {}).get("inputs") or {}
    input_map = _input_name_to_filename(entry)
    if not input_map:
        return list(spec_inputs.keys())
    names = [n for n in spec_inputs if n in input_map]
    names += [n for n in input_map if n not in spec_inputs]
    return names


def _io_declarations(
    entry: LedgerEntry,
) -> tuple[list[str], list[str], list[str]]:
    """Build Nextflow input/output declaration lines for an entry.

    Returns (input_decls, output_decls, input_names). Input names are
    the slots actually staged at run time (unmapped optional inputs are
    not declared); outputs prefer recorded filenames and fall back to
    spec patterns. Shared by docker/conda and engine process builders.
    """
    spec = entry.tool_spec_snapshot or {}
    input_names = _declared_input_names(entry)
    if not input_names:
        input_decls = ["    path input"]
    else:
        input_decls = [f"    path {name}" for name in input_names]

    output_files = _output_filenames(entry)
    if output_files:
        output_decls = [
            f"    path '{f}', emit: out{idx}"
            for idx, f in enumerate(output_files)
        ]
    else:
        output_decls = []
        for idx, out_def in enumerate((spec.get("outputs") or {}).values()):
            pattern = out_def.get("pattern") or f"*.{out_def.get('format', 'out')}"
            output_decls.append(f"    path '{pattern}', emit: out{idx}")
        if not output_decls:
            output_decls = ["    path '*', emit: out0"]

    return input_decls, output_decls, input_names


def _make_engine_nf_process(proc: str, entry: LedgerEntry) -> str:
    """Build a Nextflow process block for an engine executor entry.

    Handles snakemake, galaxy, and nextflow executors. These processes have
    NO container or conda directive — the engine runs on the host and manages
    its own containerized jobs. The script block invokes the engine CLI.
    """
    executor = entry.executor or ""
    input_decls, output_decls, input_names = _io_declarations(entry)

    # Build the engine invocation script.
    # For engine entries, the spec's command field contains the engine CLI
    # invocation (e.g. "snakemake -s ...", "planemo run ...", "nextflow run ...").
    # Render it with Jinja using Nextflow-style variables, same as docker/conda.
    script = _render_script_for_nextflow(entry, input_names)

    # Engine-specific comment for the process block, including recorded version
    # executor_version is stored as "<engine> <version>" (e.g. "snakemake 7.32.4");
    # extract just the version part to avoid duplicating the engine name.
    raw_ver = entry.executor_version or "unknown"
    ver = _extract_version(executor, raw_ver) if raw_ver != "unknown" else "unknown"
    engine_comments = {
        "snakemake": f"// Engine: snakemake {ver} (recorded at run time; runs on host, manages its own containers)",
        "galaxy":    f"// Engine: planemo/galaxy {ver} (recorded at run time; runs on host, manages its own job execution)",
        "nextflow":  f"// Engine: nextflow {ver} (recorded at run time; manages its own)",
    }
    comment = engine_comments.get(executor, f"// No container — engine {executor} {ver} runs on host")

    return f"""
process {proc} {{
    {comment}
    publishDir "results/{proc}", mode: 'copy'

    input:
{chr(10).join(input_decls)}

    output:
{chr(10).join(output_decls)}

    script:
    \"\"\"
    {script}
    \"\"\"
}}"""


def _make_nf_process(proc: str, entry: LedgerEntry) -> str:
    """Build a single Nextflow process block from a ledger entry.

    Dispatches engine executor entries (snakemake/galaxy/nextflow) to
    _make_engine_nf_process before any container/conda directive logic.
    Only docker/conda entries proceed to the existing logic.
    """
    if entry.executor in ("snakemake", "galaxy", "nextflow"):
        return _make_engine_nf_process(proc, entry)

    image = entry.container.image if entry.container else "ubuntu:latest"
    spec = entry.tool_spec_snapshot or {}

    # Determine environment directive: conda vs container
    if entry.executor == "conda":
        conda_deps = spec.get("conda_deps", [])
        env_line = f'    conda "{" ".join(conda_deps)}"'
    else:
        env_line = f"    container '{image}'"

    # Input/output declarations share logic with the engine variant —
    # each output gets an emit: name so the workflow block can reference
    # individual outputs by name.
    input_decls, output_decls, input_names = _io_declarations(entry)

    script = _render_script_for_nextflow(entry, input_names)

    return f"""
process {proc} {{
{env_line}
    publishDir "results/{proc}", mode: 'copy'

    input:
{chr(10).join(input_decls)}

    output:
{chr(10).join(output_decls)}

    script:
    \"\"\"
    {script}
    \"\"\"
}}"""


def _detect_engine_versions(entries: list[LedgerEntry]) -> dict[str, str]:
    """Detect installed engine versions for entries that use them.

    Returns a dict mapping engine name to version string (or 'not installed').
    Delegates to the shared ``versions.detect_engine_version`` which scans
    all output lines for a semver (e.g. ``nextflow -version`` prints an
    ASCII banner before the version line).
    """
    from bioledger.core.engines.versions import detect_engine_version

    engines_needed: set[str] = set()
    for entry in entries:
        if entry.executor in ("snakemake", "galaxy", "nextflow"):
            engines_needed.add(entry.executor)

    version_cmds = {
        "snakemake": "snakemake",
        "nextflow": "nextflow",
        "galaxy": "planemo",
    }
    return {
        engine: detect_engine_version(engine, version_cmds[engine])
        for engine in engines_needed
        if engine in version_cmds
    }


# Re-export from the shared versions module for backwards compatibility.
from bioledger.core.engines.versions import _extract_version  # noqa: F401,E402


def _recorded_engine_versions(entries: list[LedgerEntry]) -> dict[str, str]:
    """Collect engine versions recorded on ledger entries at submit time.

    Falls back to live detection (``_detect_engine_versions``) for engines
    present in the entries but with an empty ``executor_version`` (legacy
    entries created before the field existed).
    """
    versions: dict[str, str] = {}
    for entry in entries:
        if entry.executor in ("snakemake", "nextflow", "galaxy") and entry.executor_version:
            versions[entry.executor] = entry.executor_version
    # Fall back to live detection for engines present but with empty version
    present = {e.executor for e in entries if e.executor in ("snakemake", "nextflow", "galaxy")}
    missing = present - set(versions)
    if missing:
        live = _detect_engine_versions(entries)
        for e in missing:
            versions[e] = live.get(e, "unknown")
    return versions


def generate_engine_env_yml(entries: list[LedgerEntry]) -> str | None:
    """Generate an engine-env.yml conda env file for engine entries.

    Returns None if no engine entries are present. The YAML pins engine
    versions recorded at submit time (from ``LedgerEntry.executor_version``).
    Falls back to live detection for legacy entries without a recorded version.
    If an engine isn't installed, emits a comment instead of a dependency line.
    """
    versions = _recorded_engine_versions(entries)
    if not versions:
        return None

    lines = [
        "# Auto-generated by BioLedger — engine runtime environment",
        "# Create with: conda env create -f engine-env.yml",
        "name: bioledger-engines",
        "channels:",
        "  - bioconda",
        "  - conda-forge",
        "dependencies:",
    ]
    # The conda package that provides each engine's CLI — "galaxy" entries
    # run through planemo, and `galaxy=` is not a valid bioconda package.
    package_name = {"galaxy": "planemo"}
    for engine, version in sorted(versions.items()):
        ver = _extract_version(engine, version)
        pkg = package_name.get(engine, engine)
        if re.match(r"^\d+\.\d+", ver):
            lines.append(f"  - {pkg}={ver}")
        else:
            lines.append(f"  # {pkg}: version could not be determined")
    return "\n".join(lines) + "\n"


def _render_workflow(ordered: list[LedgerEntry]) -> str:
    """Shared renderer: emit process blocks + workflow block for ordered entries.

    For each entry's inputs, the renderer looks up which prior entry produced
    that file (via the output source map) and wires the specific named output
    channel.  Inputs not found in any prior entry's outputs are treated as root
    inputs and wired to crate channels (``${projectDir}/{entry_id[:8]}/{file}``).
    """
    if not ordered:
        return "// Empty workflow — no tool or script runs in selection"

    # The source map is built incrementally as entries are processed so a
    # consumer always wires to the most recent PRIOR producer of a given
    # filename — important when an intermediate step rewrites a file under
    # the same basename (first-wins would wire the original producer).
    source_map: dict[str, tuple[str, str]] = {}
    entry_to_proc: dict[str, str] = {}
    processes: list[str] = []
    workflow_lines: list[str] = ["workflow {"]

    for i, entry in enumerate(ordered):
        proc = _proc_name(entry, i)
        entry_to_proc[entry.id] = proc
        processes.append(_make_nf_process(proc, entry))

        # Wire each input to its correct source
        input_map = _input_name_to_filename(entry)
        input_names = _declared_input_names(entry)

        channels: list[str] = []
        for name in input_names:
            filename = input_map.get(name)
            producer = source_map.get(filename) if filename else None
            if producer is not None:
                producer_id, emit_name = producer
                producer_proc = entry_to_proc[producer_id]
                channels.append(f"{producer_proc}.out.{emit_name}")
                continue
            # Root input: wire to crate channel
            if filename:
                crate_dir = entry.id[:8]
                channels.append(
                    f'Channel.fromPath("${{projectDir}}/{crate_dir}/{filename}")'
                )
            else:
                channels.append("Channel.fromPath(params.input)")

        if not channels:
            channels.append("Channel.fromPath(params.input)")

        workflow_lines.append(f"    {proc}({', '.join(channels)})")
        _update_source_map(source_map, entry)

    workflow_lines.append("}")
    return "\n".join(processes) + "\n\n" + "\n".join(workflow_lines)


def to_nextflow(
    session: LedgerSession,
    include_running: bool = False,
) -> str:
    """Convert a ledger session into a DAG-aware Nextflow DSL2 workflow.

    Args:
        session: The session to crystallize.
        include_running: If True, also include entries with run_status
            "running" (e.g. jobs still executing). Their outputs may not
            be known yet, so downstream wiring may be incomplete.
    """
    valid_statuses = ("completed", "failed")
    if include_running:
        valid_statuses = ("completed", "failed", "running")
    tool_entries = [
        e for e in session.entries
        if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
        and e.run_status in valid_statuses
    ]
    deps = _build_dependency_graph(tool_entries)
    ordered = _topological_order_from_deps(tool_entries, deps)
    return _render_workflow(ordered)


def to_nextflow_from_entries(
    entries: list[LedgerEntry],
    include_running: bool = False,
) -> str:
    """Generate a Nextflow DSL2 workflow from a list of entries.

    Used by build_rocrate when packaging selected entries. Inputs are wired
    by matching filenames to prior entries' outputs; inputs not found in any
    prior entry's outputs are treated as root inputs from the crate layout.

    Args:
        entries: Entries to include in the workflow.
        include_running: If True, also include entries with run_status
            "running".
    """
    valid_statuses = ("completed", "failed")
    if include_running:
        valid_statuses = ("completed", "failed", "running")
    tool_entries = [
        e for e in entries
        if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
        and e.run_status in valid_statuses
    ]
    deps = _build_dependency_graph(tool_entries)
    ordered = _topological_order_from_deps(tool_entries, deps)
    return _render_workflow(ordered)
