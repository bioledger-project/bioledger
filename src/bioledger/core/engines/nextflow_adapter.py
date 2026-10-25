"""Nextflow adapter — local execution via nextflow CLI.

Implements all five ``ExecutionAdapter`` methods:
- ``submit_spec()``: run a workflow toolspec whose command invokes nextflow directly
- ``generate()``: produce a parameterized Nextflow workflow with scatter channels
- ``submit()``: bind actual values via nextflow params and launch as subprocess
- ``poll()``: check if the nextflow process is still running
- ``collect()``: discover output files from the work directory

Nextflow is a Java binary (not a Python package), so it's always detected
via PATH. The adapter degrades gracefully with a clear "java/nextflow not
found" error at invocation time, not import time.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from bioledger.core.engines.conda_derive import resolve_container_from_spec
from bioledger.core.engines.config import NextflowEngineConfig
from bioledger.core.engines.protocol import (
    RunHandle,
    RunStatus,
    WorkflowHandle,
    build_output_patterns,
    parse_collection_value,
    split_collection_inputs,
)

logger = logging.getLogger(__name__)


def _check_nextflow_available() -> str:
    """Return the nextflow executable path, or raise with install instructions."""
    exe = shutil.which("nextflow")
    if exe is None:
        raise RuntimeError(
            "nextflow is not installed. Install with: pip install bioledger[nextflow] "
            "or download from https://nextflow.io"
        )
    return exe


def _resolve_container(spec: Any) -> str:
    """Resolve the container image from spec.container or spec.conda_deps.

    .. deprecated:: Use ``resolve_container_from_spec`` from
        ``conda_derive`` directly.
    """
    return resolve_container_from_spec(spec)


def _parse_collection_value(value: str) -> tuple[str, str]:
    """Determine the channel type for a collection input value.

    .. deprecated:: Use ``parse_collection_value`` from ``protocol`` directly.
    """
    return parse_collection_value(value)


def _log_progress(run_handle: RunHandle) -> tuple[int, int]:
    """Read (completed, total) process counts from a live run's log."""
    log_path = Path(run_handle.work_dir) / "nextflow.log"
    completed = 0
    total = 0
    if log_path.exists():
        try:
            content = log_path.read_text()
            completed = content.count("Completed process")
            total = max(completed, 1)
        except Exception:
            pass
    return completed, total


def _generate_nextflow_workflow(
    spec: Any,
    input_mapping: dict[str, str],
    container_image: str,
    artifact_path: Path,
) -> tuple[list[str], list[str]]:
    """Generate a parameterized Nextflow DSL2 workflow for fan-out.

    Produces a workflow with a scatter process that fans out over a
    collection input. All paths are parameterized via ``params.*`` —
    ``submit()`` binds actual values via ``-params-file``.

    Returns (process_names, collection_inputs).
    """
    spec_outputs = getattr(spec, "outputs", {}) or {}
    command_template = getattr(spec, "command", "echo hello")
    tool_name = getattr(spec, "name", "tool")

    collection_inputs, single_inputs = split_collection_inputs(
        spec, input_mapping
    )

    # Build process name — must be a valid Nextflow identifier
    proc_name = re.sub(r"[^A-Za-z0-9_]", "_", tool_name)

    # Build input declarations
    input_decls: list[str] = []
    for name in collection_inputs:
        input_decls.append(f"    path {name}")
    for name in single_inputs:
        input_decls.append(f"    path {name}")

    if not input_decls:
        input_decls = ["    path input"]

    # Build output declarations with emit names
    output_decls: list[str] = []
    output_patterns: list[str] = []
    for idx, (out_name, out_def) in enumerate(spec_outputs.items()):
        pattern = getattr(out_def, "pattern", "") or "*.out"
        output_patterns.append(pattern)
        output_decls.append(f"    path '{pattern}', emit: out{idx}")

    if not output_decls:
        output_decls = ["    path '*', emit: out0"]
        output_patterns = ["*.out"]

    # Build container directive
    container_directive = ""
    if container_image:
        container_directive = f"    container '{container_image}'"

    # Build the workflow
    # For collection inputs, create a scatter channel from params
    channel_lines: list[str] = []
    for name in collection_inputs:
        channel_lines.append(
            f"    {name}_ch = Channel.fromPath(params.{name}_pattern)"
            f".map {{ it -> file(it) }}"
        )
    for name in single_inputs:
        channel_lines.append(
            f"    {name}_ch = Channel.fromPath(params.{name})"
        )

    if not channel_lines:
        channel_lines.append("    input_ch = Channel.fromPath(params.input)")

    # Workflow call: scatter over first collection input
    if collection_inputs:
        scatter_ch = f"{collection_inputs[0]}_ch"
        if single_inputs:
            combined = scatter_ch
            for name in single_inputs:
                combined += f".combine({name}_ch)"
            workflow_call = f"    {proc_name}({combined})"
        else:
            workflow_call = f"    {proc_name}({scatter_ch})"
    else:
        # No collection inputs — single-item dispatch
        inputs_str = ", ".join(f"{name}_ch" for name in single_inputs)
        workflow_call = (
            f"    {proc_name}({inputs_str})"
            if inputs_str
            else f"    {proc_name}(input_ch)"
        )

    # Render the command template into Nextflow syntax via Jinja:
    #   {{ inputs.X }}     -> ${X}          (staged path input)
    #   {{ parameters.X }} -> ${params.X}   (workflow param)
    #   {{ outputs.X }} /  {{ outputs.X.path }}
    #                      -> concrete filename the tool writes, e.g.
    #                         ${bam.simpleName}.sorted.bam when the pattern
    #                         contains a glob; the emit: declarations then
    #                         capture it via the pattern.
    from bioledger.core.engines._subprocess import (
        _NextflowVar,
        _OutputRef,
        nextflow_jinja_env,
    )

    all_input_names = collection_inputs + single_inputs
    first_in = all_input_names[0] if all_input_names else "input"
    # _NextflowVar so `| stem`/`| basename` on inputs emit the equivalent
    # ${x.baseName}/${x.simpleName}/${x.name} Path properties — a plain
    # "${x}" string has no extension and the filters would be no-ops.
    inputs_ctx = {n: _NextflowVar("${%s}" % n) for n in all_input_names}
    outputs_ctx: dict[str, Any] = {"_dir": "."}
    for out_name, out_def in spec_outputs.items():
        pat = getattr(out_def, "pattern", "") or "*.out"
        expr = (
            pat.replace("*", "${%s.simpleName}" % first_in)
            if "*" in pat
            else pat
        )
        outputs_ctx[out_name] = _OutputRef(expr)
    params_ctx = {
        n: "${params.%s}" % n
        for n in (getattr(spec, "parameters", {}) or {})
    }
    rendered_command = nextflow_jinja_env().from_string(
        command_template
    ).render(
        {"inputs": inputs_ctx, "outputs": outputs_ctx,
         "parameters": params_ctx}
    )

    lines = [
        "// Generated by BioLedger — fan-out for " + tool_name,
        "// Scatter axis: " + (collection_inputs[0] if collection_inputs else "none"),
        "",
        "params.input = '.'",
        "params."
        + (collection_inputs[0] if collection_inputs else "input")
        + "_pattern = './data/*'",
    ]
    for name in single_inputs:
        lines.append(f"params.{name} = './data/{name}'")

    lines.extend([
        "",
        "process " + proc_name + " {",
    ])
    if container_directive:
        lines.append(container_directive)
    lines.append("    publishDir 'results', mode: 'copy'")
    lines.append("")
    lines.append("    input:")
    lines.extend(input_decls)
    lines.append("")
    lines.append("    output:")
    lines.extend(output_decls)
    lines.append("")
    lines.append("    script:")
    lines.append('    """')
    lines.append(f"    {rendered_command}")
    lines.append('    """')
    lines.append("}")
    lines.append("")
    lines.append("workflow {")
    lines.extend(channel_lines)
    lines.append(workflow_call)
    lines.append("}")

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(lines))
    return [proc_name], collection_inputs


class NextflowAdapter:
    """Nextflow adapter for local execution via the nextflow CLI.

    Nextflow is a Java binary, not a Python package. The adapter shells out
    to the nextflow executable for submit and uses process polling for status.
    Container support is via Nextflow's native docker/singularity profiles.
    """

    _engine_name = "nextflow"

    def __init__(self, config: NextflowEngineConfig | None = None) -> None:
        self._config = config or NextflowEngineConfig()

    @property
    def executable(self) -> str:
        if self._config.executable:
            return self._config.executable
        return _check_nextflow_available()

    def _detect_version(self) -> str:
        """Detect the nextflow version by shelling out once; cache on the instance."""
        if hasattr(self, "_cached_version"):
            return self._cached_version
        from bioledger.core.engines.versions import detect_engine_version
        self._cached_version = detect_engine_version(self._engine_name, self.executable)
        return self._cached_version

    def _build_params_json(
        self,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        collection_inputs: list[str] | None = None,
    ) -> str:
        """Build a JSON params file for nextflow from input_mapping + params.

        Collection inputs get a ``<name>_pattern`` key with the glob pattern.
        Single inputs get a ``<name>`` key with the path.
        """
        coll = set(collection_inputs or [])
        result: dict[str, Any] = {}
        for k, v in {**input_mapping, **params}.items():
            key = f"{k}_pattern" if k in coll else k
            result[key] = v
        return json.dumps(result, indent=2)

    def submit_spec(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Run a single workflow toolspec via nextflow.

        Jinja-renders ``spec.command`` with real input paths and parameter
        values, then launches the rendered command as a subprocess. The
        command is expected to be a valid ``nextflow run ...`` invocation.
        """
        from bioledger.core.engines._subprocess import render_and_launch

        command_template = getattr(spec, "command", "")
        if not command_template:
            raise RuntimeError(
                "Nextflow spec has no command field — cannot run directly. "
                "Provide a valid 'nextflow run ...' invocation in the command field."
            )

        return render_and_launch(
            command_template=command_template,
            input_mapping=input_mapping,
            params=params,
            work_dir=work_dir,
            executor_name="nextflow",
            engine_version=self._detect_version(),
            log_filename="nextflow.log",
            spec=spec,
        )

    def generate(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        extra_args: list[str] | None = None,
    ) -> WorkflowHandle:
        """Generate a parameterized fan-out Nextflow workflow.

        Produces a DSL2 workflow with a scatter process that fans out over
        a collection input. The container image is baked into the process's
        container directive.
        """
        container_image = resolve_container_from_spec(spec)

        # Unique temporary artifact path — submit() writes it to work_dir.
        # NamedTemporaryFile avoids the one-second-collision of time.time() names.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="bioledger_nextflow_", suffix=".nf", delete=False
        ) as tf:
            artifact_path = Path(tf.name)

        process_names, collection_inputs = _generate_nextflow_workflow(
            spec, input_mapping, container_image, artifact_path
        )

        output_patterns = build_output_patterns(spec)

        return WorkflowHandle(
            executor="nextflow",
            artifact_path=artifact_path,
            step_inventory=process_names,
            container_image=container_image,
            input_mapping=input_mapping,
            output_patterns=output_patterns,
            metadata={
                "extra_args": extra_args or [],
                "collection_inputs": collection_inputs,
            },
        )

    def submit(
        self,
        handle: WorkflowHandle,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Submit the generated Nextflow workflow.

        Binds actual input_mapping values via a JSON params file, then
        launches nextflow as a subprocess.
        """
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Copy the workflow to the work dir
        nf_path = work_dir / "main.nf"
        nf_path.write_text(handle.artifact_path.read_text())

        # Write params.json with bound values
        params_path = work_dir / "params.json"
        collection_inputs = handle.metadata.get("collection_inputs", [])
        params_path.write_text(
            self._build_params_json(input_mapping, params, collection_inputs)
        )

        # `container '<img>'` directives are inert unless a container
        # engine is enabled in config. Auto-detect docker/singularity and
        # write nextflow.config alongside main.nf (nextflow loads it from
        # the project dir — no -c flag needed, and it travels into
        # crystallized crates as a companion file). A user-supplied
        # config_file takes full responsibility instead.
        if handle.container_image and not self._config.config_file:
            if shutil.which("docker"):
                runtime = "docker"
            elif shutil.which("singularity") or shutil.which("apptainer"):
                runtime = "singularity"
            else:
                # Nothing detected — default to docker so the failure is
                # a clear "docker not found" rather than an inert host run.
                runtime = "docker"
            (work_dir / "nextflow.config").write_text(
                "// Generated by BioLedger — enable the container runtime\n"
                "// required by the `container` directives in main.nf.\n"
                f"{runtime}.enabled = true\n"
            )

        # Build the nextflow command
        cmd = [
            self.executable,
            "run", str(nf_path),
            "-params-file", str(params_path),
            "-work-dir", str(work_dir / "work"),
        ]
        # Add config file if specified
        if self._config.config_file:
            cmd.extend(["-c", str(self._config.config_file)])
        # Add extra args from handle and config
        extra_args = handle.metadata.get("extra_args", [])
        cmd.extend(extra_args)
        cmd.extend(self._config.extra_args)

        # Launch as subprocess (non-blocking) via the shared shell wrapper —
        # gets the .exit_code EXIT-trap sidecar for free.
        from bioledger.core.engines._subprocess import launch_logged_process

        return launch_logged_process(
            shlex.join(cmd),
            work_dir=work_dir,
            executor_name="nextflow",
            log_filename="nextflow.log",
            metadata={"engine_version": self._detect_version()},
        )

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Check if the nextflow subprocess is still running.

        ``.exit_code`` (written by the launch wrapper's EXIT trap) is the
        source of truth; ``_check_completion`` (nextflow.log grepping)
        is the fallback for runs without a sidecar.
        """
        from bioledger.core.engines._subprocess import poll_local_process

        return poll_local_process(
            run_handle,
            log_filename="nextflow.log",
            fallback_check=self._check_completion,
            progress_fn=_log_progress,
        )

    def _check_completion(self, run_handle: RunHandle) -> RunStatus | None:
        """Determine completion status from log file."""
        log_path = Path(run_handle.work_dir) / "nextflow.log"

        if log_path.exists():
            content = log_path.read_text()
            if "Error" in content or "ERROR" in content:
                run_handle.status = "failed"
                return RunStatus(
                    state="failed",
                    message="See nextflow.log for details",
                )
            if "Workflow completed" in content:
                run_handle.status = "completed"
                completed = content.count("Completed process")
                return RunStatus(
                    state="completed",
                    completed=completed,
                    total=completed,
                )

        run_handle.status = "unknown"
        return RunStatus(
            state="unknown",
            message="Could not determine completion status",
        )

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """Collect outputs from a completed nextflow run.

        Discovers output files from the results/ directory and work directory.
        """
        work_dir = Path(run_handle.work_dir)
        results_dir = work_dir / "results"
        outputs: list[dict[str, Any]] = []

        if results_dir.exists():
            for f in results_dir.rglob("*"):
                if f.is_file():
                    outputs.append({
                        "path": str(f),
                        "name": f.name,
                        "size": f.stat().st_size,
                    })

        return outputs
