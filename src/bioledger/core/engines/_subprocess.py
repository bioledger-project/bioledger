"""Shared subprocess launch + poll helpers for engine adapters.

Provides:

- ``render_and_launch()`` — Jinja-renders a command template with real
  input/param/output values and launches it as a non-blocking subprocess.
  Used by all three engine adapters' ``submit_spec()`` methods for the
  direct-command path (single-tool execution).
- ``launch_logged_process()`` — launches a shell command string via
  ``/bin/sh -c`` with an EXIT trap that records the exit code to
  ``work_dir/.exit_code``. Used by ``render_and_launch()`` and by the
  Snakemake/Nextflow fan-out ``submit()`` methods.
- ``poll_local_process()`` — shared poll implementation: PID liveness
  first, then the ``.exit_code`` sidecar as source of truth, then an
  adapter-specific fallback (stats.json / engine log) for legacy runs or
  externally-killed wrappers.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from bioledger.core.engines.protocol import (
    RunHandle,
    RunStatus,
    _is_process_alive,
)

EXIT_CODE_FILENAME = ".exit_code"
EXIT_CODE_ENV_VAR = "BIOLEDGER_EXIT_CODE_PATH"


class _OutputRef(str):
    """str subclass exposing ``.path`` so both ``{{ outputs.X }}`` and
    ``{{ outputs.X.path }}`` render to the output path."""

    path: str

    def __new__(cls, path: str) -> "_OutputRef":
        obj = super().__new__(cls, path)
        obj.path = path
        return obj


class _NextflowVar(str):
    """A ``${name}`` placeholder standing in for a Nextflow ``path`` input.

    Renders exactly like a plain string, but is recognized by
    :func:`nextflow_jinja_env`'s filters. A literal ``"${bam}"`` has no
    file extension, so running the shared ``stem``/``basename`` filters on
    it is a no-op — the correct output is the equivalent Nextflow ``Path``
    property (``${bam.baseName}``, ``${bam.simpleName}``, ``${bam.name}``),
    which Nextflow's GString resolves against the staged file.
    """


_NF_VAR_RE = re.compile(r"^\$\{(\w+)\}$")


def nextflow_jinja_env():
    """Jinja env where path filters map ``${var}`` onto Nextflow Path props.

    Same filters as the shared toolspec env, but ``stem``/``basename``/
    ``splitext`` detect :class:`_NextflowVar` placeholders and emit Groovy
    expressions instead of processing the literal text:

    - ``{{ inputs.x | stem }}``       → ``${x.baseName}``   (last ext only)
    - ``{{ inputs.x | stem(true) }}`` → ``${x.simpleName}`` (all exts)
    - ``{{ inputs.x | basename }}``   → ``${x.name}``
    - ``{{ inputs.x | splitext }}``   → ``[${x.baseName}, ${x.extension}]``

    Any other value flows through to the original filter unchanged.
    """
    from bioledger.toolspec import get_jinja_env

    env = get_jinja_env().overlay()
    env.filters = dict(env.filters)  # detach before overriding
    base_stem = env.filters["stem"]
    base_basename = env.filters["basename"]
    base_splitext = env.filters["splitext"]

    def _nf_prop(value: object, prop: str) -> _NextflowVar | None:
        m = _NF_VAR_RE.match(str(value))
        if isinstance(value, _NextflowVar) and m:
            return _NextflowVar(f"${{{m.group(1)}.{prop}}}")
        return None

    def _stem(value: object, all: bool = False) -> object:  # noqa: A002
        prop = "simpleName" if all else "baseName"
        return _nf_prop(value, prop) or base_stem(str(value), all)

    def _basename(value: object) -> object:
        return _nf_prop(value, "name") or base_basename(str(value))

    def _splitext(value: object) -> object:
        m = _NF_VAR_RE.match(str(value))
        if isinstance(value, _NextflowVar) and m:
            name = m.group(1)
            return [
                _NextflowVar(f"${{{name}.baseName}}"),
                _NextflowVar(f"${{{name}.extension}}"),
            ]
        return base_splitext(str(value))

    env.filters["stem"] = _stem
    env.filters["basename"] = _basename
    env.filters["splitext"] = _splitext
    return env


def _build_render_context(
    input_mapping: dict[str, str],
    params: dict[str, Any],
    work_dir: Path,
    spec: Any | None = None,
) -> dict[str, Any]:
    """Build the Jinja context for a command template.

    When ``spec`` (an ExecutionSpec or compatible object) is provided:

    - required inputs missing from ``input_mapping`` raise ``ValueError``
      before anything is launched;
    - optional inputs absent from ``input_mapping`` render as ``""`` so
      ``{% if inputs.opt %}`` conditionals work without StrictUndefined;
    - ``outputs.<name>`` / ``outputs.<name>.path`` resolve to
      ``work_dir/<pattern>``;
    - parameter defaults pre-populate ``parameters``.
    """
    inputs_ctx: dict[str, str] = dict(input_mapping)
    outputs_ctx: dict[str, Any] = {"_dir": str(work_dir)}
    parameters_ctx: dict[str, Any] = {}

    if spec is not None:
        spec_inputs = getattr(spec, "inputs", {}) or {}
        missing_required = [
            name
            for name, idef in spec_inputs.items()
            if getattr(idef, "required", True) and name not in input_mapping
        ]
        if missing_required:
            raise ValueError(
                f"Missing required input(s) for "
                f"'{getattr(spec, 'name', 'spec')}': "
                + ", ".join(sorted(missing_required))
            )
        for name in spec_inputs:
            inputs_ctx.setdefault(name, "")

        for name, odef in (getattr(spec, "outputs", {}) or {}).items():
            pattern = getattr(odef, "pattern", "") or name
            outputs_ctx[name] = _OutputRef(str(work_dir / pattern))

        for name, pdef in (getattr(spec, "parameters", {}) or {}).items():
            default = getattr(pdef, "default", None)
            if default is not None:
                parameters_ctx[name] = default

    parameters_ctx.update(params)
    return {
        "inputs": inputs_ctx,
        "outputs": outputs_ctx,
        "parameters": parameters_ctx,
    }


def launch_logged_process(
    shell_command: str,
    work_dir: Path,
    executor_name: str,
    log_filename: str = "engine.log",
    metadata: dict[str, Any] | None = None,
) -> RunHandle:
    """Launch ``shell_command`` via ``/bin/sh -c`` and return a RunHandle.

    An EXIT trap writes the exit code to ``work_dir/.exit_code``. The
    destination is passed through the environment
    (``BIOLEDGER_EXIT_CODE_PATH``) rather than interpolated into the trap
    string, so spaces/quotes in the path — or a ``cd`` inside the command
    — can't break it. A stale ``.exit_code`` from a previous run in the
    same directory is removed first.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    exit_code_path = (work_dir / EXIT_CODE_FILENAME).resolve()
    exit_code_path.unlink(missing_ok=True)

    env = dict(os.environ)
    env[EXIT_CODE_ENV_VAR] = str(exit_code_path)

    wrapped = (
        "trap 'echo $? > \"$" + EXIT_CODE_ENV_VAR + "\"' EXIT\n" + shell_command
    )

    with open(work_dir / log_filename, "w") as log_file:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", wrapped],
            cwd=str(work_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    process_start_time = None
    try:
        import psutil

        process_start_time = psutil.Process(proc.pid).create_time()
    except Exception:
        pass

    return RunHandle(
        executor=executor_name,
        run_id=str(proc.pid),
        entry_id="",
        work_dir=work_dir,
        status="running",
        process_start_time=process_start_time,
        metadata=metadata or {},
    )


def render_and_launch(
    command_template: str,
    input_mapping: dict[str, str],
    params: dict[str, Any],
    work_dir: Path,
    executor_name: str,
    engine_version: str = "",
    log_filename: str = "engine.log",
    spec: Any | None = None,
) -> RunHandle:
    """Jinja-render a command template with real values and launch as subprocess.

    Args:
        command_template: Jinja2-templated command string (uses
            ``{{inputs.X}}``, ``{{outputs._dir}}``, ``{{parameters.X}}``).
        input_mapping: Mapping of input names to real filesystem paths.
        params: Mapping of parameter names to real values.
        work_dir: Working directory for the subprocess.
        executor_name: Engine name for the RunHandle (e.g. "nextflow").
        engine_version: Version string for metadata.
        log_filename: Name of the log file inside work_dir.
        spec: Optional ExecutionSpec — when given, required inputs are
            validated, absent optional inputs render as ``""``, and
            ``outputs.<name>.path`` resolves to ``work_dir/<pattern>``.

    Returns:
        RunHandle with the subprocess PID and metadata.
    """
    from bioledger.toolspec import get_jinja_env

    work_dir = Path(work_dir)

    context = _build_render_context(input_mapping, params, work_dir, spec)
    rendered_cmd = get_jinja_env().from_string(command_template).render(context)

    # Resolve ${projectDir} to the actual work_dir for live execution.
    # In crystallized workflows, ${projectDir} is a Nextflow variable;
    # for direct subprocess execution it must be a real path.
    rendered_cmd = rendered_cmd.replace("${projectDir}", str(work_dir))

    return launch_logged_process(
        rendered_cmd,
        work_dir=work_dir,
        executor_name=executor_name,
        log_filename=log_filename,
        metadata={"engine_version": engine_version},
    )


def read_exit_code(work_dir: Path) -> int | None:
    """Read the ``.exit_code`` sidecar, or None if absent/unparseable."""
    try:
        return int((Path(work_dir) / EXIT_CODE_FILENAME).read_text().strip())
    except (OSError, ValueError):
        return None


def log_fallback(
    log_filename: str,
    error_markers: tuple[str, ...] = ("Error", "ERROR"),
    success_markers: tuple[str, ...] = (),
) -> Callable[[RunHandle], RunStatus]:
    """Build a fallback_check that greps a log file when ``.exit_code``
    is absent (e.g. the wrapper shell was SIGKILLed, or the run predates
    the sidecar mechanism)."""
    def _check(run_handle: RunHandle) -> RunStatus:
        log_path = Path(run_handle.work_dir) / log_filename
        if log_path.exists():
            content = log_path.read_text(errors="replace")
            if any(m in content for m in error_markers):
                run_handle.status = "failed"
                return RunStatus(
                    state="failed",
                    message=f"See {log_filename} for details",
                )
            if success_markers and any(m in content for m in success_markers):
                run_handle.status = "completed"
                return RunStatus(state="completed")
        run_handle.status = "unknown"
        return RunStatus(
            state="unknown",
            message="Could not determine completion status",
        )

    return _check


def poll_local_process(
    run_handle: RunHandle,
    log_filename: str,
    fallback_check: Callable[[RunHandle], RunStatus | None] | None = None,
    progress_fn: Callable[[RunHandle], tuple[int, int]] | None = None,
) -> RunStatus | None:
    """Poll a local process launched by ``launch_logged_process()``.

    Check order:

    1. ``run_handle.run_id`` unparseable → ``None`` (lost run).
    2. Process alive → ``RunStatus(state="running")`` with progress from
       ``progress_fn`` when provided.
    3. Process gone → ``.exit_code`` sidecar decides completed/failed.
    4. No sidecar (SIGKILLed wrapper shell, or a run predating the
       sidecar mechanism) → ``fallback_check`` if given, else ``unknown``.
    """
    try:
        pid = int(run_handle.run_id)
    except (ValueError, TypeError):
        return None

    if _is_process_alive(pid, run_handle.process_start_time):
        completed = total = 0
        if progress_fn is not None:
            try:
                completed, total = progress_fn(run_handle)
            except Exception:
                pass
        return RunStatus(
            state="running",
            completed=completed,
            total=total,
            message="",
        )

    code = read_exit_code(run_handle.work_dir)
    if code is not None:
        run_handle.status = "completed" if code == 0 else "failed"
        return RunStatus(
            state=run_handle.status,
            message=(
                ""
                if code == 0
                else f"Process exited with code {code} (see {log_filename})"
            ),
        )

    if fallback_check is not None:
        status = fallback_check(run_handle)
        if status is not None:
            return status

    run_handle.status = "unknown"
    return RunStatus(
        state="unknown",
        message="Could not determine completion status",
    )
