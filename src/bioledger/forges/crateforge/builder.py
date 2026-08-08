from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from rocrate.model import ContextEntity
from rocrate.rocrate import ROCrate

from bioledger.ledger.models import EntryKind, LedgerEntry, LedgerSession

logger = logging.getLogger(__name__)


def _detect_executors(entries: list[LedgerEntry]) -> set[str]:
    """Detect which executors are present in the selected entries."""
    executors: set[str] = set()
    for e in entries:
        if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
            executors.add(e.executor)
    return executors


def _is_single_engine(executors: set[str]) -> bool:
    """Check if all executors are docker/conda (single-engine, no workflow engine)."""
    return executors <= {"docker", "conda"}


def _add_engine_artifact(
    crate: ROCrate,
    scratch: Path,
    entries: list[LedgerEntry],
    executor: str,
    include_running: bool,
) -> str | None:
    """Add a workflow artifact for a specific engine to the crate.

    Returns the dest_path of the artifact, or None if no entries for this engine.
    """
    engine_entries = [e for e in entries if e.executor == executor]

    if not engine_entries:
        return None

    if executor in ("docker", "conda"):
        from bioledger.forges.analysisforge.crystallize import (
            to_nextflow_from_entries,
        )

        nf_content = to_nextflow_from_entries(
            engine_entries, include_running=include_running
        )
        nf_path = scratch / "workflow.nf"
        nf_path.write_text(nf_content)
        crate.add_workflow(
            str(nf_path),
            dest_path="workflow.nf",
            main=False,
            lang="nextflow",
        )
        return "workflow.nf"

    elif executor == "snakemake":
        snakefile_paths: list[str] = []
        for e in engine_entries:
            snakefile_content = _generate_snakefile_from_entry(e)
            if snakefile_content:
                tool_name = e.tool_spec_name or "tool"
                rule_name = tool_name.replace("-", "_")
                dest = f"{rule_name}.smk"
                sf_path = scratch / dest
                sf_path.write_text(snakefile_content)
                crate.add_file(
                    str(sf_path),
                    dest_path=dest,
                    properties={
                        "description": f"Snakemake rule for {tool_name}",
                        "programmingLanguage": "snakemake",
                    },
                )
                snakefile_paths.append(dest)

        if snakefile_paths:
            if len(snakefile_paths) == 1:
                return snakefile_paths[0]
            else:
                master_content = "\n".join(
                    f'include: "{p}"' for p in snakefile_paths
                )
                master_path = scratch / "Snakefile"
                master_path.write_text(master_content)
                crate.add_file(
                    str(master_path),
                    dest_path="Snakefile",
                    properties={
                        "description": "Master Snakefile (includes all rules)",
                        "programmingLanguage": "snakemake",
                    },
                )
                return "Snakefile"

    elif executor == "nextflow":
        from bioledger.forges.analysisforge.crystallize import (
            to_nextflow_from_entries,
        )

        nf_content = to_nextflow_from_entries(
            engine_entries, include_running=include_running
        )
        nf_path = scratch / "engine_nextflow.nf"
        nf_path.write_text(nf_content)
        crate.add_workflow(
            str(nf_path),
            dest_path="engine_nextflow.nf",
            main=False,
            lang="nextflow",
        )
        return "engine_nextflow.nf"

    elif executor == "galaxy":
        from bioledger.forges.analysisforge.crystallize import (
            to_galaxy_workflow,
        )

        temp_session = LedgerSession(name="galaxy_subset")
        for e in engine_entries:
            temp_session.add(e)
        ga_workflow = to_galaxy_workflow(temp_session)
        ga_path = scratch / "workflow.ga"
        ga_path.write_text(json.dumps(ga_workflow, indent=2))
        crate.add_file(
            str(ga_path),
            dest_path="workflow.ga",
            properties={
                "description": "Galaxy workflow definition",
                "programmingLanguage": "galaxy",
            },
        )
        return "workflow.ga"

    return None


def _generate_snakefile_from_entry(entry: LedgerEntry) -> str | None:
    """Generate a Snakefile representation from a fan-out ledger entry."""
    from bioledger.core.engines.conda_derive import resolve_container_from_spec
    from bioledger.core.engines.snakemake_adapter import _generate_snakefile
    from bioledger.toolspec.models import ExecutionSpec

    spec_snapshot = entry.tool_spec_snapshot or {}
    if not spec_snapshot:
        return None

    try:
        spec = ExecutionSpec(**spec_snapshot)
    except Exception:
        return None

    input_mapping = entry.container.input_mapping if entry.container else {}
    container_image = resolve_container_from_spec(spec)

    with tempfile.NamedTemporaryFile(suffix=".smk", delete=False) as tmp:
        artifact_path = Path(tmp.name)

    try:
        _generate_snakefile(spec, input_mapping, container_image, artifact_path)
        return artifact_path.read_text()
    except Exception as e:
        logger.warning("Failed to generate Snakefile from entry %s: %s", entry.id, e)
        return None
    finally:
        artifact_path.unlink(missing_ok=True)


def _build_composite_entity(
    crate: ROCrate,
    artifact_paths: list[str],
    session: LedgerSession,
    entries: list[LedgerEntry],
) -> ContextEntity:
    """Build a composite workflow entity as the crate's mainEntity."""
    has_part = [{"@id": p} for p in artifact_paths]

    dep_graph = session.dependency_graph()
    steps_desc = []
    for entry in entries:
        if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
            continue
        deps = dep_graph.get(entry.id, set())
        dep_str = ", ".join(sorted(deps)) if deps else "root"
        steps_desc.append(
            f"{entry.id[:8]}:{entry.tool_spec_name} "
            f"(deps: {dep_str}, executor: {entry.executor})"
        )

    composite = ContextEntity(
        crate,
        "composite-workflow",
        properties={
            "@type": ["ComputationalWorkflow", "SoftwareSourceCode"],
            "name": f"BioLedger composite workflow for session {session.id[:8]}",
            "description": (
                f"Multi-engine workflow with {len(artifact_paths)} "
                f"engine artifact(s). Steps:\n" + "\n".join(steps_desc)
            ),
            "programmingLanguage": "multi",
            "url": "https://bioledger.dev/composite-workflow",
            "hasPart": has_part,
        },
    )
    return composite


def build_rocrate(
    session: LedgerSession,
    output_dir: Path,
    entry_ids: list[str] | None = None,
    include_running: bool = False,
) -> Path:
    """Package a ledger session (or subset) as an RO-Crate.

    Args:
        session: The full session
        output_dir: Where to write crate files
        entry_ids: If provided, only include these entries (user-selected subset).
                   If None, include all entries.
        include_running: If True, include entries with run_status "running"
                         in the Nextflow workflow.

    The RO-Crate includes:
    - Data files consumed/produced by selected entries
    - A workflow (Nextflow, Snakemake, Galaxy, or composite) as mainEntity
    - A ledger.json with provenance for selected entries

    For single-engine sessions (docker/conda only), the workflow is a
    crystallized Nextflow DSL2 workflow — the existing path, unchanged.

    For multi-engine sessions (entries from 2+ distinct executors), a
    composite workflow entity is synthesized as the mainEntity. Each
    engine's native artifact (workflow.nf, *.smk, workflow.ga) is added
    as a supplementary (non-main) file, and the composite entity references
    them via hasPart. This avoids the rocrate library's single-value
    mainEntity limitation (last add_workflow(main=True) would overwrite
    prior ones).
    """
    crate = ROCrate()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Filter entries if subset requested
    if entry_ids is not None:
        selected_ids = set(entry_ids)
        entries = [e for e in session.entries if e.id in selected_ids]
    else:
        entries = session.entries

    # Add data files from selected entries
    added_files: set[str] = set()
    for entry in entries:
        for fref in entry.files:
            dedup_key = f"{entry.id}:{fref.path}"
            if dedup_key in added_files:
                continue
            p = Path(fref.path)
            if p.exists():
                dest = f"{entry.id[:8]}/{p.name}"
                crate.add_file(
                    str(p),
                    dest_path=dest,
                    properties={
                        "description": f"{fref.role} for {entry.kind.value}",
                        "sha256": fref.sha256,
                    },
                )
                added_files.add(dedup_key)

    # Generated files (workflow.nf, nextflow.config, ledger.json) need to live
    # on disk for the RO-Crate library to read and copy into the final crate.
    # Use a temp directory so these scratch files don't leak into output_dir
    # alongside the actual crate — if they did, users could accidentally run
    # `nextflow run workflow.nf` from output_dir and pick up a stale copy that
    # can't find the input files (which are inside the crate subdir).
    with tempfile.TemporaryDirectory() as scratch_dir:
        scratch = Path(scratch_dir)

        tool_entries = [
            e for e in entries if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
        ]
        if tool_entries:
            executors = _detect_executors(tool_entries)

            if _is_single_engine(executors):
                # Single-engine path: crystallize into Nextflow as mainEntity
                from bioledger.forges.analysisforge.crystallize import (
                    to_nextflow_from_entries,
                )

                nf_content = to_nextflow_from_entries(
                    tool_entries, include_running=include_running
                )
                nf_path = scratch / "workflow.nf"
                nf_path.write_text(nf_content)
                crate.add_workflow(
                    str(nf_path),
                    dest_path="workflow.nf",
                    main=True,
                    lang="nextflow",
                )

                config_content = (
                    "// Auto-generated by BioLedger: run each process inside the\n"
                    "// container declared in its process block. Override with\n"
                    "// `-profile <name>` or edit this file if you need a different\n"
                    "// execution environment.\n"
                    "docker {\n"
                    "    enabled = true\n"
                    "}\n"
                )
                config_path = scratch / "nextflow.config"
                config_path.write_text(config_content)
                crate.add_file(
                    str(config_path),
                    dest_path="nextflow.config",
                    properties={
                        "description": "Nextflow configuration for running the workflow"
                    },
                )
            else:
                # Multi-engine path: add each engine's artifact as non-main,
                # then synthesize a composite workflow entity as mainEntity.
                artifact_paths: list[str] = []

                for executor in sorted(executors):
                    artifact_path = _add_engine_artifact(
                        crate, scratch, tool_entries,
                        executor, include_running,
                    )
                    if artifact_path:
                        artifact_paths.append(artifact_path)

                if artifact_paths:
                    # Add nextflow.config if any Nextflow artifacts present
                    if any(p.endswith(".nf") for p in artifact_paths):
                        config_content = (
                            "// Auto-generated by BioLedger\n"
                            "docker {\n"
                            "    enabled = true\n"
                            "}\n"
                        )
                        config_path = scratch / "nextflow.config"
                        config_path.write_text(config_content)
                        crate.add_file(
                            str(config_path),
                            dest_path="nextflow.config",
                            properties={
                                "description": "Nextflow configuration"
                            },
                        )

                    # Build and set composite entity as mainEntity
                    composite = _build_composite_entity(
                        crate, artifact_paths, session, entries
                    )
                    crate.add(composite)
                    crate.mainEntity = composite

        # Add filtered ledger as provenance
        filtered_session = session.model_copy()
        filtered_session.entries = entries
        ledger_path = scratch / "ledger.json"
        ledger_path.write_text(filtered_session.model_dump_json(indent=2))
        crate.add_file(
            str(ledger_path),
            dest_path="ledger.json",
            properties={"description": "BioLedger provenance log (selected entries)"},
        )

        crate_dir = output_dir / "ro-crate"
        crate.write(str(crate_dir))

    return crate_dir
