from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from bioledger.core.containers.docker import DockerRunner, RunResult
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.toolspec import get_jinja_env
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


# Reuse the canonical Jinja2 environment with BioLedger custom filters
_jinja_env = get_jinja_env()


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
            if exclude_paths and key in exclude_paths:
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
                    if not child.is_file():
                        continue
                    child_key = str(child.resolve())
                    if child_key in seen:
                        continue
                    if child_key in staged_input_paths:
                        continue
                    if exclude_paths and child_key in exclude_paths:
                        continue
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


def _to_shell_command(rendered_cmd: str) -> list[str]:
    """Decide whether a rendered command needs a shell wrapper (pipes,
    redirects, etc.) or can be split into an argv list directly."""
    shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
    if any(op in rendered_cmd for op in shell_operators):
        return ["sh", "-c", rendered_cmd]
    try:
        return shlex.split(rendered_cmd)
    except ValueError:
        return ["sh", "-c", rendered_cmd]


def _build_input_file_refs(input_mapping: dict[str, str], run_dir: Path) -> list[FileRef]:
    """Build FileRefs for staged inputs. Path is the STAGED path (basename
    matches input_mapping and the crystallize/RO-Crate layout); hashing
    follows the symlink to the real content."""
    file_refs = []
    for staged_basename in input_mapping.values():
        staged_path = run_dir / staged_basename
        if staged_path.exists():
            file_refs.append(
                FileRef(
                    path=str(staged_path), sha256=_hash_file(staged_path),
                    size_bytes=staged_path.stat().st_size, role="input",
                )
            )
    return file_refs


def _finalize_entry(
    entry: LedgerEntry,
    spec: ToolSpec,
    run_dir: Path,
    result: RunResult,
    pre_snapshot: dict[str, float],
    staged_input_paths: set[str],
) -> LedgerEntry:
    """Populate an entry's outputs/logs/status from a completed RunResult.

    Shared by the blocking path (immediately after runner.run()/poll())
    and the async poll path (poll_tool(), once the container reaches a
    terminal state) so output discovery/hashing logic isn't duplicated.
    Mutates and returns the same entry (LedgerEntry is not frozen).
    """
    log_paths = _persist_logs(run_dir, result)
    # Also exclude the pre-snapshot sidecar so it doesn't leak as a spurious
    # output via the fallback discovery path.
    snapshot_path = run_dir / "_pre_snapshot.json"
    if snapshot_path.exists():
        log_paths.add(str(snapshot_path.resolve()))

    output_paths = _discover_outputs(
        spec, run_dir, pre_snapshot, staged_input_paths, exclude_paths=log_paths
    )
    for out_path in output_paths:
        entry.files.append(
            FileRef(
                path=str(out_path), sha256=_hash_file(out_path),
                size_bytes=out_path.stat().st_size, role="output",
            )
        )

    for name in _LOG_NAMES:
        path = run_dir / name
        if path.exists():
            entry.files.append(
                FileRef(
                    path=str(path), sha256=_hash_file(path),
                    size_bytes=path.stat().st_size, role="log",
                )
            )

    entry.exit_code = result.exit_code
    entry.duration_seconds = result.duration_seconds
    entry.run_status = "completed" if result.exit_code == 0 else "failed"
    return entry


def _submit_and_stage(
    session: LedgerSession,
    spec: ToolSpec,
    input_files: dict[str, Path],
    session_dir: Path,
    params: dict | None,
    parent_id: str | None,
) -> tuple[LedgerEntry, Path, dict[str, float], set[str], DockerRunner]:
    """Shared setup for submit_tool()/run_tool(): stage inputs, snapshot the
    run dir BEFORE starting the container (so fast-finishing tools don't
    race the pre-snapshot), then start the container.

    Returns (entry, run_dir, pre_snapshot, staged_input_paths, runner) —
    the latter two are needed by run_tool()'s caller to finalize the entry
    once the container completes; submit_tool() discards them since
    poll_tool() reconstructs an equivalent snapshot later from disk.
    """
    runner = DockerRunner()

    run_id = uuid.uuid4().hex[:20]
    run_dir = session_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Stage inputs (symlink for in-session sources, copy for external)
    input_mapping = _stage_inputs(input_files, session_dir, run_dir)

    # Snapshot BEFORE launching the container — must precede submit() so
    # instant-finishing tools can't write outputs before we've recorded
    # the "before" state.
    pre_snapshot = _snapshot_dir(run_dir)
    staged_input_paths = {str((run_dir / name).resolve()) for name in input_mapping.values()}

    # Persist the pre-snapshot so poll_tool() can read it back across a
    # process boundary instead of reconstructing from current mtimes
    # (which is fragile if a tool modifies its inputs in-place).
    (run_dir / "_pre_snapshot.json").write_text(json.dumps(pre_snapshot))

    # Mount full session dir so relative symlinks to sibling run dirs resolve.
    volumes = {str(session_dir): {"bind": "/sessions", "mode": "rw"}}
    container_run_dir = f"/sessions/runs/{run_id}"

    rendered_cmd = _render_command(
        spec, input_mapping, params or {}, work_mount=container_run_dir
    )
    command = _to_shell_command(rendered_cmd)

    container_id = runner.submit(
        image=spec.container,
        command=command,
        volumes=volumes,
        workdir=container_run_dir,
    )

    file_refs = _build_input_file_refs(input_mapping, run_dir)

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
        run_status="running",
        container_id=container_id,
    )
    session.add(entry)
    return entry, run_dir, pre_snapshot, staged_input_paths, runner


def submit_tool(
    session: LedgerSession,
    spec: ToolSpec,
    input_files: dict[str, Path],
    session_dir: Path,
    params: dict | None = None,
    parent_id: str | None = None,
) -> LedgerEntry:
    """Start a tool run in the background and return immediately.

    The returned entry has run_status="running" and container_id set.
    Call poll_tool() later — from this process or a fresh one — to check
    on and eventually finalize it.
    """
    entry, _run_dir, _pre_snapshot, _staged, _runner = _submit_and_stage(
        session, spec, input_files, session_dir, params, parent_id
    )
    return entry


def poll_tool(entry: LedgerEntry, session_dir: Path) -> LedgerEntry:
    """Check a submitted tool run for completion. Idempotent.

    If entry.run_status != "running", returns it unchanged. If the
    container is still running, returns it unchanged. Once the container
    reaches a terminal state, finalizes the entry (discovers outputs,
    hashes files, sets exit_code/duration_seconds/run_status) in place and
    returns it.

    Safe to call from a different process than the one that submitted the
    job: everything needed is reconstructed from the entry + session_dir.
    """
    if entry.run_status != "running" or not entry.container_id:
        return entry

    runner = DockerRunner()
    result = runner.poll(entry.container_id)
    if result is None:
        return entry

    run_dir = session_dir / "runs" / entry.id
    spec = ToolSpec(execution=ExecutionSpec(**(entry.tool_spec_snapshot or {})))
    input_mapping = entry.container.input_mapping if entry.container else {}
    staged_input_paths = {
        str((run_dir / name).resolve()) for name in input_mapping.values()
    }

    # Read the pre-snapshot persisted at submit time. Falls back to
    # reconstructing from current mtimes for entries submitted before
    # this file existed (backward compatibility).
    snapshot_path = run_dir / "_pre_snapshot.json"
    if snapshot_path.exists():
        pre_snapshot = json.loads(snapshot_path.read_text())
    else:
        pre_snapshot = {
            p: Path(p).stat().st_mtime for p in staged_input_paths if Path(p).exists()
        }

    return _finalize_entry(entry, spec, run_dir, result, pre_snapshot, staged_input_paths)


def run_tool(
    session: LedgerSession,
    spec: ToolSpec,
    input_files: dict[str, Path],
    session_dir: Path,
    params: dict | None = None,
    parent_id: str | None = None,
    timeout: int = 3600,
    poll_interval: float = 0.5,
) -> tuple[LedgerEntry, RunResult]:
    """Execute a tool via Docker in an isolated per-run working directory,
    blocking until it finishes.

    Each run gets its own directory under sessions/<id>/runs/<entry_id>/,
    mounted at /sessions/runs/<entry_id>. Inputs are staged as symlinks
    (for in-session sources) or copies (for external sources). Outputs are
    discovered via declared pattern globs.

    Implemented as submit_tool() + a local poll loop (see DockerRunner.run
    for why a single long HTTP wait is avoided). Signature/behavior are
    unchanged for existing callers.

    Args:
        session: The active ledger session.
        spec: Tool specification to execute.
        input_files: Mapping of tool input names to resolved host paths.
        session_dir: The session's root directory (for staging decisions).
        params: Tool parameter overrides.
        parent_id: Optional parent entry ID for DAG linkage.
    """
    entry, run_dir, pre_snapshot, staged_input_paths, runner = _submit_and_stage(
        session, spec, input_files, session_dir, params, parent_id
    )

    start = time.monotonic()
    result = runner.poll(entry.container_id)
    while result is None:
        if time.monotonic() - start > timeout:
            try:
                container = runner.client.containers.get(entry.container_id)
                container.kill()
            except Exception:
                pass
            try:
                container = runner.client.containers.get(entry.container_id)
                container.remove(force=True)
            except Exception:
                pass
            entry.run_status = "failed"
            entry.exit_code = -1
            raise TimeoutError(
                f"Container {entry.container_id} exceeded timeout of {timeout}s"
            )
        time.sleep(poll_interval)
        result = runner.poll(entry.container_id)

    _finalize_entry(entry, spec, run_dir, result, pre_snapshot, staged_input_paths)
    return entry, result


def run_script(
    session: LedgerSession,
    script_path: Path,
    session_dir: Path,
    container: str = "python:3.11-slim",
    input_files: dict[str, Path] | None = None,
    parent_id: str | None = None,
    timeout: int = 3600,
    poll_interval: float = 0.5,
) -> tuple[LedgerEntry, RunResult]:
    """Run a custom script in a container using an isolated per-run directory.

    Uses the same submit/poll pattern as run_tool() for unified timeout
    and container lifecycle handling.
    """
    input_files = input_files or {}

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
    staged_input_paths = {str((run_dir / name).resolve()) for name in input_mapping.values()}

    # Persist the pre-snapshot (same rationale as _submit_and_stage)
    (run_dir / "_pre_snapshot.json").write_text(json.dumps(pre_snapshot))

    # Mount script dir (ro) + full session dir (rw so symlinks resolve)
    volumes = {
        str(script_path.parent): {"bind": "/scripts", "mode": "ro"},
        str(session_dir): {"bind": "/sessions", "mode": "rw"},
    }

    runner = DockerRunner()
    container_id = runner.submit(
        image=container,
        command=["python", f"/scripts/{script_path.name}"],
        volumes=volumes,
        workdir=f"/sessions/runs/{run_id}",
    )

    # Build entry with script + input file refs; _finalize_entry appends
    # outputs, logs, exit_code, duration, and run_status.
    file_refs = [
        FileRef(
            path=str(script_path), sha256=_hash_file(script_path),
            size_bytes=script_path.stat().st_size, role="script",
        ),
    ]
    file_refs.extend(_build_input_file_refs(input_mapping, run_dir))

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
        run_status="running",
        container_id=container_id,
    )
    session.add(entry)

    # Poll loop — same pattern as run_tool
    start = time.monotonic()
    result = runner.poll(container_id)
    while result is None:
        if time.monotonic() - start > timeout:
            try:
                c = runner.client.containers.get(container_id)
                c.kill()
            except Exception:
                pass
            try:
                c = runner.client.containers.get(container_id)
                c.remove(force=True)
            except Exception:
                pass
            entry.run_status = "failed"
            entry.exit_code = -1
            raise TimeoutError(
                f"Container {container_id} exceeded timeout of {timeout}s"
            )
        time.sleep(poll_interval)
        result = runner.poll(container_id)

    _finalize_entry(entry, spec, run_dir, result, pre_snapshot, staged_input_paths)
    return entry, result
