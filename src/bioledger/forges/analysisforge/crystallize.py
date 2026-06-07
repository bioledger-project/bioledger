from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from jinja2 import Template

from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession

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
        return Template(template_str).render(context)
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
    # fall back to tool spec patterns.
    output_files = _output_filenames(entry)
    if output_files:
        output_decls = [f"    path '{f}'" for f in output_files]
    else:
        output_decls = []
        for out_def in (spec.get("outputs") or {}).values():
            pattern = out_def.get("pattern") or f"*.{out_def.get('format', 'out')}"
            output_decls.append(f"    path '{pattern}'")
        if not output_decls:
            output_decls = ["    path '*'"]

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


def _crate_input_channels(entry: LedgerEntry) -> list[str]:
    """Build Nextflow channel expressions for an entry's inputs, referencing
    their location within the packaged RO-Crate.

    The crate layout places each entry's files at ``{entry_id[:8]}/{filename}``
    relative to the crate root (where ``workflow.nf`` lives).
    """
    spec_inputs = (entry.tool_spec_snapshot or {}).get("inputs") or {}
    input_map = _input_name_to_filename(entry)
    ordered_names = list(spec_inputs.keys()) or list(input_map.keys())
    crate_dir = entry.id[:8]
    channels: list[str] = []
    for name in ordered_names:
        filename = input_map.get(name)
        if filename:
            channels.append(
                f'Channel.fromPath("${{projectDir}}/{crate_dir}/{filename}")'
            )
        else:
            channels.append("Channel.fromPath(params.input)")
    if not channels:
        # No declared inputs — fall back to a single generic channel so the
        # process can still be wired.
        channels.append("Channel.fromPath(params.input)")
    return channels


def _render_workflow(ordered: list[LedgerEntry]) -> str:
    """Shared renderer: emit process blocks + workflow block for ordered entries.

    Root entries (parent not in selection) reference their specific input files
    from within the packaged RO-Crate. Chained entries consume their parent's
    output channel.
    """
    if not ordered:
        return "// Empty workflow — no tool or script runs in selection"

    entry_to_proc: dict[str, str] = {}
    processes: list[str] = []
    workflow_lines: list[str] = ["workflow {"]

    for i, entry in enumerate(ordered):
        proc = _proc_name(entry, i)
        entry_to_proc[entry.id] = proc
        processes.append(_make_nf_process(proc, entry))

        if entry.parent_id and entry.parent_id in entry_to_proc:
            parent_proc = entry_to_proc[entry.parent_id]
            workflow_lines.append(f"    {proc}({parent_proc}.out)")
        else:
            # Root entry: wire to its specific input files from the crate
            channels = _crate_input_channels(entry)
            workflow_lines.append(f"    {proc}({', '.join(channels)})")

    workflow_lines.append("}")
    return "\n".join(processes) + "\n\n" + "\n".join(workflow_lines)


def to_nextflow(session: LedgerSession) -> str:
    """Convert a ledger session into a DAG-aware Nextflow DSL2 workflow."""
    children, _by_id = _build_dag(session)
    ordered = _topological_order(children)
    return _render_workflow(ordered)


def to_nextflow_from_entries(entries: list[LedgerEntry]) -> str:
    """Generate a Nextflow DSL2 workflow from a list of entries.

    Used by build_rocrate when packaging selected entries. Entries whose
    parent_id is not in the subset are treated as roots — their inputs are
    read from the packaged crate layout. Multiple roots represent independent
    parallel branches, which is a valid Nextflow pattern.
    """
    tool_entries = [
        e for e in entries if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
    ]
    return _render_workflow(tool_entries)


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
