from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import uuid
from pathlib import Path
from typing import Any

from jinja2 import Environment

from bioledger.core.containers.docker import DockerRunner, RunResult
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.toolspec.models import (
    ExecutionSpec,
    ParamType,
    SpecStatus,
    ToolInput,
    ToolSpec,
)


# File names used when persisting container stdout/stderr to disk.
_LOG_NAMES = ("stdout.log", "stderr.log")


def _persist_logs(run_dir: Path, result: RunResult) -> set[str]:
    """Write container stdout/stderr to run_dir and return resolved paths.

    These paths are used to exclude the log files from output discovery,
    since they are written after the pre_snapshot and would otherwise be
    picked up as new/modified files by the fallback discovery.
    """
    log_paths: set[str] = set()
    for name, text in (("stdout.log", result.stdout), ("stderr.log", result.stderr)):
        path = run_dir / name
        path.write_text(text, encoding="utf-8")
        log_paths.add(str(path.resolve()))
    return log_paths


# Custom Jinja2 filters for Galaxy-converted templates
def _basename(path: str) -> str:
    """Get the filename from a path (like Python's os.path.basename)."""
    return os.path.basename(path)


def _splitext(path: str) -> list[str]:
    """Split path into [root, ext] (like Python's os.path.splitext)."""
    root, ext = os.path.splitext(path)
    return [root, ext]


def _stem(path: str, all: bool = False) -> str:
    """Get the filename without extension (like pathlib.Path.stem).

    When ``all`` is True, iteratively strip all extensions (e.g.
    ``reference.fna.gz`` → ``reference``). When False (default), only the
    last extension is removed.
    """
    basename = os.path.basename(path)
    if not all:
        return os.path.splitext(basename)[0]
    while True:
        root, ext = os.path.splitext(basename)
        if not ext or root == "":
            break
        basename = root
    return basename


# Jinja2 environment with custom filters
_jinja_env = Environment()
_jinja_env.filters["basename"] = _basename
_jinja_env.filters["splitext"] = _splitext
_jinja_env.filters["stem"] = _stem


def _hash_file(path: Path, chunk_size: int = 8192) -> str:
    """Stream-hash a file (safe for multi-GB BAM files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_dir(work_dir: Path) -> dict[str, float]:
    """Record mtime of every file currently in work_dir (recursive).
    Used to detect new/changed files after a tool run.

    Keys are resolved (symlink-followed) absolute paths so they align with
    _discover_outputs, which also keys by resolved path. This matters because
    staged inputs may be symlinks pointing into other run directories.
    """
    snapshot: dict[str, float] = {}
    for p in work_dir.rglob("*"):
        if p.is_file():
            snapshot[str(p.resolve())] = p.stat().st_mtime
    return snapshot


def _stage_inputs(
    input_files: dict[str, Path],
    session_dir: Path,
    run_dir: Path,
) -> dict[str, str]:
    """Stage inputs into the run directory with collision-aware naming.

    Each input is staged under its source basename by default. If multiple
    inputs would share the same basename, they are disambiguated by prefixing
    with their input slot name: <slot>__<basename>.

    Staging strategy:
    - Sources inside session_dir: symlink (zero cost, resolves in container
      because the full session tree is mounted).
    - Sources outside session_dir: copy (can't be symlinked into the mount).

    Args:
        input_files: Mapping of input slot name -> source Path.
        session_dir: The session's root directory (for symlink vs copy decision).
        run_dir: The per-run directory where inputs are staged.

    Returns:
        Mapping of input slot name -> staged basename (the filename within run_dir).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    session_resolved = session_dir.resolve()

    # First pass: determine basenames and detect collisions
    basenames: dict[str, str] = {}  # slot -> basename
    basename_counts: dict[str, list[str]] = {}  # basename -> [slots]

    for slot, src in input_files.items():
        basename = src.name
        basenames[slot] = basename
        basename_counts.setdefault(basename, []).append(slot)

    # Determine final staged names (collision-aware)
    staged_names: dict[str, str] = {}
    for slot, basename in basenames.items():
        colliding_slots = basename_counts[basename]
        if len(colliding_slots) > 1:
            # Collision: prefix with slot name
            staged_names[slot] = f"{slot}__{basename}"
        else:
            staged_names[slot] = basename

    # Second pass: create symlinks or copies
    run_resolved = run_dir.resolve()
    for slot, src in input_files.items():
        staged_name = staged_names[slot]
        dest = run_dir / staged_name

        # Resolve source to check if it's inside session_dir
        src_resolved = src.resolve()

        # If already in run_dir (shouldn't happen in normal flow), skip
        if str(src_resolved).startswith(str(run_resolved) + os.sep):
            continue

        # Check if source is inside session_dir (eligible for symlink)
        in_session = str(src_resolved).startswith(str(session_resolved) + os.sep)

        if dest.exists() or dest.is_symlink():
            # Remove existing to allow idempotent restaging
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            elif dest.is_dir():
                shutil.rmtree(dest)

        if in_session:
            # Relative symlink so it resolves inside the container, where the
            # session tree is mounted at a different prefix (/sessions). An
            # absolute host-path target would dangle in-container.
            rel_target = os.path.relpath(src_resolved, run_resolved)
            os.symlink(rel_target, dest)
        elif src_resolved.is_dir():
            # Directory input (e.g. rtg-tools SDF template) — copy the tree.
            shutil.copytree(src_resolved, dest)
        else:
            # Copy external file source.
            shutil.copy2(src_resolved, dest)

    # Return mapping: slot -> staged basename
    return {slot: staged_names[slot] for slot in input_files}


def _discover_outputs(
    spec: ToolSpec,
    run_dir: Path,
    pre_snapshot: dict[str, float],
    staged_input_paths: set[str],
    exclude_paths: set[str] | None = None,
) -> list[Path]:
    """Discover output files using declared output patterns + change detection.

    Returns paths of new/modified files matching any declared output pattern.
    Staged inputs (symlinks or copies) are excluded from the fallback to
    prevent input files leaking into the output list.
    """
    output_paths: list[Path] = []
    seen: set[str] = set()

    # Use declared patterns from spec
    for _name, out in spec.execution.outputs.items():
        pattern = out.pattern
        if not pattern:
            continue
        # Support directory-type outputs (trailing /)
        is_dir_pattern = pattern.endswith("/")
        if is_dir_pattern:
            pattern = pattern.rstrip("/")

        for match in run_dir.rglob(pattern):
            key = str(match.resolve())
            if key in seen:
                continue
            # Only include new/modified files
            if match.is_file():
                old_mtime = pre_snapshot.get(key)
                if old_mtime is None or match.stat().st_mtime > old_mtime:
                    output_paths.append(match)
                    seen.add(key)
            elif match.is_dir() and is_dir_pattern:
                # Capture all files within a directory output
                for child in match.rglob("*"):
                    if child.is_file():
                        child_key = str(child.resolve())
                        if child_key not in seen:
                            output_paths.append(child)
                            seen.add(child_key)

    # Fallback: if spec declares no patterns (or none matched), capture any
    # new/modified files as outputs (excluding staged inputs)
    if not output_paths:
        for p in run_dir.rglob("*"):
            if not p.is_file():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            if key in staged_input_paths:
                continue
            if exclude_paths and key in exclude_paths:
                continue
            old_mtime = pre_snapshot.get(key)
            if old_mtime is None or p.stat().st_mtime > old_mtime:
                output_paths.append(p)
                seen.add(key)

    return output_paths


def _render_command(
    spec: ToolSpec,
    input_mapping: dict[str, str],
    params: dict,
    work_mount: str = "/work",
) -> str:
    """Render the Jinja2 command template with concrete values.

    inputs.<name> resolves to /work/<staged-basename> (or empty string if
    optional + absent), per the input_mapping produced by _stage_inputs.
    outputs._dir resolves to /work.
    outputs.<name>.path resolves to /work/<pattern>.
    """
    outputs_ctx: dict[str, Any] = {"_dir": work_mount}
    for name, out in spec.execution.outputs.items():
        outputs_ctx[name] = {"path": f"{work_mount}/{out.pattern}"}

    # Build inputs context — absent optionals render as empty string
    inputs_ctx: dict[str, str] = {}
    for name in spec.execution.inputs:
        rel = input_mapping.get(name)
        inputs_ctx[name] = f"{work_mount}/{rel}" if rel else ""

    context = {
        "inputs": inputs_ctx,
        "parameters": {
            **{name: p.default for name, p in spec.execution.parameters.items()},
            **params,
        },
        "outputs": outputs_ctx,
    }
    return _jinja_env.from_string(spec.execution.command).render(context)


def get_session_dir(home_dir: Path, session_id: str) -> Path:
    """Return the session root directory (not creating it)."""
    return home_dir / "sessions" / session_id


def get_tool_run_dir(home_dir: Path, session_id: str, run_id: str) -> Path:
    """Return (and create) the per-run working directory for a tool execution."""
    run_dir = home_dir / "sessions" / session_id / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def run_tool(
    session: LedgerSession,
    spec: ToolSpec,
    input_files: dict[str, Path],
    session_dir: Path,
    params: dict | None = None,
    parent_id: str | None = None,
) -> tuple[LedgerEntry, RunResult]:
    """Execute a tool via Docker in an isolated per-run working directory.

    Each run gets its own directory under sessions/<id>/runs/<entry_id>/,
    mounted at /work. Inputs are staged as symlinks (for in-session sources)
    or copies (for external sources). Outputs are discovered via declared
    pattern globs.

    Args:
        session: The active ledger session.
        spec: Tool specification to execute.
        input_files: Mapping of tool input names to resolved host paths.
        session_dir: The session's root directory (for staging decisions).
        params: Tool parameter overrides.
        parent_id: Optional parent entry ID for DAG linkage.
    """
    runner = DockerRunner()

    # Generate run_id and create isolated run directory
    run_id = uuid.uuid4().hex[:20]
    run_dir = session_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Stage inputs (symlink for in-session sources, copy for external)
    input_mapping = _stage_inputs(input_files, session_dir, run_dir)

    # Snapshot empty run_dir for change detection
    pre_snapshot = _snapshot_dir(run_dir)

    # Build staged input path set for fallback exclusion
    staged_input_paths = {str((run_dir / name).resolve()) for name in input_mapping.values()}

    # Mount full session dir so relative symlinks to sibling run dirs resolve.
    volumes = {str(session_dir): {"bind": "/sessions", "mode": "rw"}}
    container_run_dir = f"/sessions/runs/{run_id}"

    # Render command with paths rooted at the container's run directory.
    rendered_cmd = _render_command(
        spec, input_mapping, params or {}, work_mount=container_run_dir
    )

    # Shell detection
    shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
    needs_shell = any(op in rendered_cmd for op in shell_operators)

    if needs_shell:
        command = ["sh", "-c", rendered_cmd]
    else:
        try:
            command = shlex.split(rendered_cmd)
        except ValueError:
            command = ["sh", "-c", rendered_cmd]

    # Run with workdir at the run directory (inside the mounted session tree)
    result = runner.run(
        image=spec.container,
        command=command,
        volumes=volumes,
        workdir=f"/sessions/runs/{run_id}",
    )

    # Persist full stdout/stderr for later inspection and LLM diagnosis.
    log_paths = _persist_logs(run_dir, result)

    # Build input file refs. Path is the STAGED path (basename matches
    # input_mapping and the crystallize/RO-Crate layout); hashing follows the
    # symlink to the real content.
    file_refs = []
    for name, staged_basename in input_mapping.items():
        staged_path = run_dir / staged_basename
        if staged_path.exists():
            file_refs.append(
                FileRef(
                    path=str(staged_path), sha256=_hash_file(staged_path),
                    size_bytes=staged_path.stat().st_size, role="input",
                )
            )

    # Discover and record outputs via pattern globs
    output_paths = _discover_outputs(
        spec, run_dir, pre_snapshot, staged_input_paths, exclude_paths=log_paths
    )
    for out_path in output_paths:
        # Output paths are inside run_dir; store absolute path
        file_refs.append(
            FileRef(
                path=str(out_path), sha256=_hash_file(out_path),
                size_bytes=out_path.stat().st_size, role="output",
            )
        )

    # Attach log file refs so the full stdout/stderr are tracked in the ledger.
    for name in _LOG_NAMES:
        path = run_dir / name
        if path.exists():
            file_refs.append(
                FileRef(
                    path=str(path), sha256=_hash_file(path),
                    size_bytes=path.stat().st_size, role="log",
                )
            )

    entry = LedgerEntry(
        id=run_id,
        kind=EntryKind.TOOL_RUN,
        parent_id=parent_id,
        tool_spec_name=spec.name,
        tool_spec_snapshot=spec.execution.model_dump(),
        container=ContainerInfo(
            image=spec.container,
            command=command,
            volumes={k: v["bind"] for k, v in volumes.items()},
            input_mapping=input_mapping,
        ),
        files=file_refs,
        params=params or {},
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
    )
    session.add(entry)
    return entry, result


def run_script(
    session: LedgerSession,
    script_path: Path,
    session_dir: Path,
    container: str = "python:3.11-slim",
    input_files: dict[str, Path] | None = None,
    parent_id: str | None = None,
) -> tuple[LedgerEntry, RunResult]:
    """Run a custom script in a container using an isolated per-run directory."""
    input_files = input_files or {}
    runner = DockerRunner()

    # Generate run_id and create isolated run directory
    run_id = uuid.uuid4().hex[:20]
    run_dir = session_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Build transient spec
    spec = ToolSpec(
        execution=ExecutionSpec(
            name=f"script_{script_path.stem}",
            container=container,
            command=f"python /scripts/{script_path.name}",
            inputs={"script": ToolInput(type=ParamType.FILE, format="any")},
            status=SpecStatus.DRAFT,
        )
    )

    # Stage inputs (symlink for in-session sources, copy for external)
    input_mapping = _stage_inputs(input_files, session_dir, run_dir)

    # Snapshot empty run_dir for change detection
    pre_snapshot = _snapshot_dir(run_dir)

    # Build staged input path set for fallback exclusion
    staged_input_paths = {str((run_dir / name).resolve()) for name in input_mapping.values()}

    # Mount script dir (ro) + full session dir (rw so symlinks resolve)
    volumes = {
        str(script_path.parent): {"bind": "/scripts", "mode": "ro"},
        str(session_dir): {"bind": "/sessions", "mode": "rw"},
    }

    result = runner.run(
        image=container,
        command=["python", f"/scripts/{script_path.name}"],
        volumes=volumes,
        workdir=f"/sessions/runs/{run_id}",
    )

    # Persist full stdout/stderr for later inspection.
    log_paths = _persist_logs(run_dir, result)

    # Capture script + input files + outputs
    file_refs = [
        FileRef(
            path=str(script_path), sha256=_hash_file(script_path),
            size_bytes=script_path.stat().st_size, role="script",
        ),
    ]
    for name, staged_basename in input_mapping.items():
        staged_path = run_dir / staged_basename
        if staged_path.exists():
            # Staged path (basename matches input_mapping); hash follows symlink.
            file_refs.append(
                FileRef(
                    path=str(staged_path), sha256=_hash_file(staged_path),
                    size_bytes=staged_path.stat().st_size, role="input",
                )
            )

    # Discover outputs
    output_paths = _discover_outputs(
        spec, run_dir, pre_snapshot, staged_input_paths, exclude_paths=log_paths
    )
    for out_path in output_paths:
        file_refs.append(
            FileRef(
                path=str(out_path), sha256=_hash_file(out_path),
                size_bytes=out_path.stat().st_size, role="output",
            )
        )

    # Attach log file refs.
    for name in _LOG_NAMES:
        path = run_dir / name
        if path.exists():
            file_refs.append(
                FileRef(
                    path=str(path), sha256=_hash_file(path),
                    size_bytes=path.stat().st_size, role="log",
                )
            )

    entry = LedgerEntry(
        id=run_id,
        kind=EntryKind.SCRIPT_RUN,
        parent_id=parent_id,
        tool_spec_name=spec.name,
        tool_spec_snapshot=spec.execution.model_dump(),
        container=ContainerInfo(
            image=container,
            command=["python", f"/scripts/{script_path.name}"],
            volumes={k: v["bind"] for k, v in volumes.items()},
            input_mapping=input_mapping,
        ),
        files=file_refs,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
    )
    session.add(entry)
    return entry, result
