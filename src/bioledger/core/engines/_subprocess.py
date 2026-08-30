"""Shared subprocess launch helper for engine adapters.

Provides ``render_and_launch()`` — Jinja-renders a command template with real
input/param/output values and launches it as a non-blocking subprocess. Used
by all three engine adapters' ``submit_spec()`` methods for the direct-command
path (single-tool execution).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from bioledger.core.engines.protocol import RunHandle


def render_and_launch(
    command_template: str,
    input_mapping: dict[str, str],
    params: dict[str, Any],
    work_dir: Path,
    executor_name: str,
    engine_version: str = "",
    log_filename: str = "engine.log",
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

    Returns:
        RunHandle with the subprocess PID and metadata.
    """
    from bioledger.toolspec import get_jinja_env

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    context = {
        "inputs": input_mapping,
        "outputs": {"_dir": str(work_dir)},
        "parameters": params,
    }
    rendered_cmd = get_jinja_env().from_string(command_template).render(context)

    # Resolve ${projectDir} to the actual work_dir for live execution.
    # In crystallized workflows, ${projectDir} is a Nextflow variable;
    # for direct subprocess execution it must be a real path.
    rendered_cmd = rendered_cmd.replace("${projectDir}", str(work_dir))

    log_path = work_dir / log_filename
    log_file = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            shlex.split(rendered_cmd),
            cwd=str(work_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()

    run_id = str(proc.pid)
    process_start_time = None
    try:
        import psutil

        process_start_time = psutil.Process(proc.pid).create_time()
    except Exception:
        pass

    return RunHandle(
        executor=executor_name,
        run_id=run_id,
        entry_id="",
        work_dir=work_dir,
        status="running",
        process_start_time=process_start_time,
        metadata={"engine_version": engine_version},
    )
