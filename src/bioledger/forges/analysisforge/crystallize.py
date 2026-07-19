from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession
from bioledger.toolspec import get_jinja_env

logger = logging.getLogger(__name__)


def _build_dag(
    session: LedgerSession,
) -> tuple[
    dict[str | None, list[LedgerEntry]],  # parent_id → children
    dict[str, LedgerEntry],  # id → entry
]:
    """Build adjacency list from parent_id links."""
    children: dict[str | None, list[LedgerEntry]] = defaultdict(list)
    by_id: dict[str, LedgerEntry] = {}
    for entry in session.entries:
        if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
            if entry.run_status not in ("completed", "failed"):
                continue
            children[entry.parent_id].append(entry)
            by_id[entry.id] = entry
    return children, by_id


def _topological_order(
    children: dict[str | None, list[LedgerEntry]],
) -> list[LedgerEntry]:
    """Topological sort respecting parent_id dependencies."""
    visited: set[str] = set()
    order: list[LedgerEntry] = []

    def dfs(entry: LedgerEntry) -> None:
        if entry.id in visited:
            return
        visited.add(entry.id)
        for child in children.get(entry.id, []):
            dfs(child)
        order.append(entry)

    # Start from root entries (parent_id is None)
    for root_entry in children.get(None, []):
        dfs(root_entry)
    return list(reversed(order))


def _build_output_source_map(
    entries: list[LedgerEntry],
) -> dict[str, tuple[str, str]]:
    """Build a map from output filename → (entry_id, emit_name).

    If multiple entries produce the same filename, the first one in the
    list wins (entries are typically in execution order).

    Files that appear as both input and output in the same entry with the
    SAME sha256 are excluded — they are staged inputs that leaked into the
    output list (the tool didn't produce them, they were just staged there).

    However, if the sha256 DIFFERS (e.g. bgzip re-compresses a .fna.gz to
    a .fna.gz with different content), the output is legitimate and is
    included so downstream tools can chain to it.
    """
    source_map: dict[str, tuple[str, str]] = {}
    for entry in entries:
        # Map basename → sha256 for inputs, to detect staged-input leaks
        input_sha: dict[str, str] = {}
        for f in entry.files:
            if f.role == "input":
                input_sha[Path(f.path).name] = f.sha256
        output_files = _output_filenames(entry)
        for idx, filename in enumerate(output_files):
            # Skip staged inputs that leaked into outputs: same basename
            # AND same sha256 means the tool didn't produce it.
            if filename in input_sha:
                output_sha = next(
                    (f.sha256 for f in entry.files
                     if f.role == "output" and Path(f.path).name == filename),
                    "",
                )
                if output_sha == input_sha[filename]:
                    continue
            if filename not in source_map:
                source_map[filename] = (entry.id, f"out{idx}")
    return source_map


def _build_dependency_graph(
    entries: list[LedgerEntry],
    source_map: dict[str, tuple[str, str]],
) -> dict[str, set[str]]:
    """Build a dependency graph based on file-level input/output matching.

    Entry A depends on entry B if A consumes a file that B produces.
    This replaces the single parent_id link with multi-source dependencies.
    """
    deps: dict[str, set[str]] = {e.id: set() for e in entries}
    for entry in entries:
        input_map = _input_name_to_filename(entry)
        for filename in input_map.values():
            if filename in source_map:
                producer_id, _ = source_map[filename]
                if producer_id != entry.id:
                    deps[entry.id].add(producer_id)
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
    return f"step_{index}_{base}".replace("-", "_")


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


def _render_script_for_nextflow(entry: LedgerEntry, input_names: list[str]) -> str:
    """Re-render the tool spec command template with Nextflow-style variables.

    In Nextflow, declared `path <name>` inputs are staged into the work dir with
    their original filename and are referenced as the bash variable ``${name}``.
    Output files in the work dir (``.``) are captured by the output declaration.
    """
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
    context = {
        "inputs": {name: f"${{{name}}}" for name in input_names},
        "outputs": {"_dir": "."},
        "parameters": params_dict,
    }
    try:
        return get_jinja_env().from_string(template_str).render(context)
    except Exception:
        logger.warning("Failed to render Nextflow script for %s", entry.id)
        if entry.container and entry.container.command:
            return " ".join(entry.container.command)
        return "echo 'render failed'"


def _make_nf_process(proc: str, entry: LedgerEntry) -> str:
    """Build a single Nextflow process block from a ledger entry."""
    image = entry.container.image if entry.container else "ubuntu:latest"
    spec = entry.tool_spec_snapshot or {}

    # Input names: prefer the tool spec's declared inputs; fall back to the
    # recovered mapping from container volumes.
    spec_inputs = spec.get("inputs") or {}
    input_map = _input_name_to_filename(entry)
    input_names = list(spec_inputs.keys()) or list(input_map.keys())
    if not input_names:
        input_decls = ["    path input"]
    else:
        input_decls = [f"    path {name}" for name in input_names]

    # Output declarations: prefer concrete filenames from the recorded run;
    # fall back to tool spec patterns.  Each output gets an emit: name so
    # the workflow block can reference individual outputs by name.
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

    script = _render_script_for_nextflow(entry, input_names)

    return f"""
process {proc} {{
    container '{image}'
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


def _render_workflow(ordered: list[LedgerEntry]) -> str:
    """Shared renderer: emit process blocks + workflow block for ordered entries.

    For each entry's inputs, the renderer looks up which prior entry produced
    that file (via the output source map) and wires the specific named output
    channel.  Inputs not found in any prior entry's outputs are treated as root
    inputs and wired to crate channels (``${projectDir}/{entry_id[:8]}/{file}``).
    """
    if not ordered:
        return "// Empty workflow — no tool or script runs in selection"

    source_map = _build_output_source_map(ordered)
    entry_to_proc: dict[str, str] = {}
    processes: list[str] = []
    workflow_lines: list[str] = ["workflow {"]

    for i, entry in enumerate(ordered):
        proc = _proc_name(entry, i)
        entry_to_proc[entry.id] = proc
        processes.append(_make_nf_process(proc, entry))

        # Wire each input to its correct source
        spec_inputs = (entry.tool_spec_snapshot or {}).get("inputs") or {}
        input_map = _input_name_to_filename(entry)
        input_names = list(spec_inputs.keys()) or list(input_map.keys())

        channels: list[str] = []
        for name in input_names:
            filename = input_map.get(name)
            if filename and filename in source_map:
                producer_id, emit_name = source_map[filename]
                # Skip self-references: an entry's input may share a basename
                # with its own output (e.g. bgzip input and output are both
                # .fna.gz). Wire to crate channel instead.
                if producer_id != entry.id and producer_id in entry_to_proc:
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
    source_map = _build_output_source_map(tool_entries)
    deps = _build_dependency_graph(tool_entries, source_map)
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
    source_map = _build_output_source_map(tool_entries)
    deps = _build_dependency_graph(tool_entries, source_map)
    ordered = _topological_order_from_deps(tool_entries, deps)
    return _render_workflow(ordered)


def to_galaxy_workflow(session: LedgerSession) -> dict:
    """Convert a ledger session into a DAG-aware Galaxy .ga workflow JSON."""
    children, by_id = _build_dag(session)
    ordered = _topological_order(children)

    entry_to_step: dict[str, int] = {}
    steps = {}

    for i, entry in enumerate(ordered):
        entry_to_step[entry.id] = i
        tool_id = entry.tool_spec_name or (
            entry.container.image.split("/")[-1].split(":")[0]
            if entry.container
            else "unknown"
        )
        tool_version = (
            entry.container.image.split(":")[-1]
            if entry.container and ":" in entry.container.image
            else "latest"
        )

        # Build input connections from parent_id
        input_connections = {}
        if entry.parent_id and entry.parent_id in entry_to_step:
            parent_step = entry_to_step[entry.parent_id]
            input_connections["input"] = {"id": parent_step, "output_name": "output"}

        steps[str(i)] = {
            "id": i,
            "type": "tool",
            "tool_id": tool_id,
            "tool_version": tool_version,
            "input_connections": input_connections,
            "position": {"left": 200 * i, "top": 200},
        }

    return {
        "a_galaxy_workflow": "true",
        "format-version": "0.1",
        "name": f"BioLedger Session {session.id}",
        "steps": steps,
    }
