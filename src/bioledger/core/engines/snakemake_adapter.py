"""Snakemake adapter — reference implementation for local execution.

Implements all five ``ExecutionAdapter`` methods for the simplest case:
local execution, no scheduler plugin, no conda mapping. Container injection
is via `--use-apptainer` (boolean enable flag) with the image baked into
the generated Snakefile's `container:` directive.

Snakemake is imported lazily so this module loads without the `bioledger[snakemake]`
extra installed. The import error surfaces at call time with install instructions.
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

import yaml

from bioledger.core.engines.conda_derive import resolve_container_from_spec
from bioledger.core.engines.config import SnakemakeEngineConfig
from bioledger.core.engines.protocol import (
    RunHandle,
    RunStatus,
    WorkflowHandle,
    build_output_patterns,
    parse_collection_value,
    split_collection_inputs,
)

logger = logging.getLogger(__name__)


def _check_snakemake_available() -> str:
    """Return the snakemake executable path, or raise with install instructions."""
    exe = shutil.which("snakemake")
    if exe is None:
        raise RuntimeError(
            "snakemake is not installed. Install with: pip install bioledger[snakemake]"
        )
    return exe


def _resolve_container(spec: Any) -> str:
    """Resolve the container image from spec.container or spec.conda_deps.

    .. deprecated:: Use ``resolve_container_from_spec`` from
        ``conda_derive`` directly. Kept as a thin wrapper for
        backward compatibility with ``builder.py`` imports.
    """
    return resolve_container_from_spec(spec)


def _parse_collection_value(value: str) -> tuple[str, str]:
    """Determine the channel type for a collection input value.

    .. deprecated:: Use ``parse_collection_value`` from ``protocol`` directly.
    """
    return parse_collection_value(value)


def _stats_progress(run_handle: RunHandle) -> tuple[int, int]:
    """Read (completed, total) job counts from a live run's stats.json."""
    stats_path = Path(run_handle.work_dir) / "stats.json"
    completed = 0
    total = 0
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text())
            total_stats = stats.get("total", {})
            total = total_stats.get("jobs", 0)
            completed = sum(
                v for k, v in total_stats.get("exit-code", {}).items()
                if k == "0"
            )
        except (json.JSONDecodeError, KeyError):
            pass
    return completed, total


def _generate_snakefile(
    spec: Any,
    input_mapping: dict[str, str],
    container_image: str,
    artifact_path: Path,
) -> tuple[list[str], list[str]]:
    """Generate a parameterized Snakefile for fan-out.

    Produces a *valid* Snakefile: top-level Python does sample discovery
    with ``glob.glob`` (``glob`` is not a Snakemake builtin), ``rule all``
    uses ``expand()`` over discovered samples, collection inputs use
    ``lambda wildcards:`` lookups, and the ``shell:`` block gets the
    command rendered into ``{input.*}`` / ``{output.*}`` / ``{params.*}``
    placeholders — raw ``{{ jinja }}`` never reaches Snakemake.

    The Snakefile uses ``config`` (from config.yaml written at submit time)
    for all path values — no hardcoded paths in the generated file.

    Returns (rule_names, collection_inputs).
    """
    from bioledger.toolspec import get_jinja_env

    spec_outputs = getattr(spec, "outputs", {}) or {}
    spec_params = getattr(spec, "parameters", {}) or {}
    command_template = getattr(spec, "command", "echo hello")
    tool_name = getattr(spec, "name", "tool")

    collection_inputs, single_inputs = split_collection_inputs(
        spec, input_mapping
    )
    scatter = collection_inputs[0] if collection_inputs else None
    wc = "sample"

    # Output patterns — {sample} wildcard substituted for expand()
    output_items: list[tuple[str, str]] = []
    for out_name, out_def in spec_outputs.items():
        pattern = getattr(out_def, "pattern", "") or f"{{{wc}}}.{out_name}"
        if "*" in pattern:
            pattern = pattern.replace("*", "{" + wc + "}")
        elif "{" not in pattern:
            pattern = "{" + wc + "}" + pattern
        safe_name = re.sub(r"\W", "_", out_name)
        output_items.append((safe_name, pattern))
    if not output_items:
        output_items = [("out", "{" + wc + "}.out")]

    # Render the Jinja command template into Snakemake placeholders.
    # Filters like | stem on the scatter input map onto the wildcard —
    # the wildcard IS the stem of each matched file.
    if scatter:
        command_template = re.sub(
            r"\{\{\s*inputs\." + re.escape(scatter)
            + r"\s*\|\s*(?:stem|basename|splitext)[^}]*\}\}",
            "{wildcards." + wc + "}",
            command_template,
        )
    inputs_ctx = {
        n: "{input.%s}" % n for n in collection_inputs + single_inputs
    }
    outputs_ctx: dict[str, Any] = {"_dir": "."}
    outputs_ctx.update({n: "{output.%s}" % n for n, _ in output_items})
    params_ctx = {n: "{params.%s}" % n for n in spec_params}
    rendered_cmd = get_jinja_env().from_string(command_template).render(
        {"inputs": inputs_ctx, "outputs": outputs_ctx,
         "parameters": params_ctx}
    )

    rule_name = re.sub(r"\W", "_", tool_name)

    lines: list[str] = [
        "# Generated by BioLedger — fan-out for " + tool_name,
        "# Scatter axis: " + wc
        + " (from collection input(s): "
        + ", ".join(collection_inputs) + ")",
        "",
        "import glob as _glob",
        "import os as _os",
        "",
        "configfile: 'config.yaml'",
        "",
    ]
    if scatter:
        lines += [
            f'_MATCHED = sorted(_glob.glob(config["{scatter}_pattern"]))',
            "_SAMPLES = [_os.path.splitext(_os.path.basename(p))[0] "
            "for p in _MATCHED]",
            "_INPUT_MAP = dict(zip(_SAMPLES, _MATCHED))",
            "",
        ]

    lines += ["rule all:", "    input:"]
    if scatter:
        pats = ", ".join(f'"{p}"' for _, p in output_items)
        lines.append(f"        expand([{pats}], {wc}=_SAMPLES),")
    else:
        for _, pat in output_items:
            lines.append(f'        "{pat}",')

    lines += ["", "rule " + rule_name + ":", "    input:"]
    input_lines: list[str] = []
    for name in collection_inputs:
        if name == scatter:
            input_lines.append(
                f"        {name}=lambda wildcards: "
                f"_INPUT_MAP[wildcards.{wc}],"
            )
        else:
            # Secondary collection inputs pair files per-sample by
            # substituting the wildcard into their glob pattern.
            input_lines.append(
                f"        {name}=lambda wildcards: "
                f'_glob.glob(config["{name}_pattern"]'
                f".replace(\"*\", wildcards.{wc}))[0],"
            )
    for name in single_inputs:
        if name in input_mapping:
            input_lines.append(f'        {name}=config["{name}"],')
    lines.extend(input_lines if input_lines else ['        "",'])

    lines.append("    output:")
    for i, (name, pat) in enumerate(output_items):
        comma = "," if i < len(output_items) - 1 else ""
        lines.append(f'        {name}="{pat}"{comma}')

    if spec_params:
        lines.append("    params:")
        for i, name in enumerate(spec_params):
            comma = "," if i < len(spec_params) - 1 else ""
            lines.append(f'        {name}=config["{name}"]{comma}')

    if container_image:
        lines.append(f'    container: "{container_image}"')

    lines += [
        "    shell:",
        '        """',
        "        " + rendered_cmd,
        '        """',
        "",
    ]

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(lines))
    return [rule_name], collection_inputs


class SnakemakeAdapter:
    """Reference Snakemake adapter for local execution.

    Implements the ExecutionAdapter protocol for the simplest case:
    local execution, no scheduler plugin, no conda mapping.
    """

    _engine_name = "snakemake"

    def __init__(self, config: SnakemakeEngineConfig | None = None) -> None:
        self._config = config or SnakemakeEngineConfig()

    @property
    def executable(self) -> str:
        if self._config.executable:
            return self._config.executable
        return _check_snakemake_available()

    def _detect_version(self) -> str:
        """Detect the snakemake version by shelling out once; cache on the instance."""
        if hasattr(self, "_cached_version"):
            return self._cached_version
        from bioledger.core.engines.versions import detect_engine_version
        self._cached_version = detect_engine_version(self._engine_name, self.executable)
        return self._cached_version

    def _build_config_yaml(
        self,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        collection_inputs: list[str] | None = None,
    ) -> str:
        """Build a config.yaml for snakemake from input_mapping + params.

        Collection inputs get a ``<name>_pattern`` key with the glob pattern.
        Single inputs get a ``<name>`` key with the path.
        """
        coll = set(collection_inputs or [])
        data: dict[str, Any] = {}
        for k, v in {**input_mapping, **params}.items():
            data[f"{k}_pattern" if k in coll else k] = v
        return yaml.safe_dump(data, sort_keys=False)

    def submit_spec(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Run a single workflow toolspec via snakemake.

        Jinja-renders ``spec.command`` with real input paths and parameter
        values, then launches the rendered command as a subprocess. The
        command is expected to be a valid ``snakemake ...`` invocation.
        """
        from bioledger.core.engines._subprocess import render_and_launch

        command_template = getattr(spec, "command", "")
        if not command_template:
            raise RuntimeError(
                "Snakemake spec has no command field — cannot run directly. "
                "Provide a valid 'snakemake ...' invocation in the command field."
            )

        return render_and_launch(
            command_template=command_template,
            input_mapping=input_mapping,
            params=params,
            work_dir=work_dir,
            executor_name="snakemake",
            engine_version=self._detect_version(),
            log_filename="snakemake.log",
            spec=spec,
        )

    def generate(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        extra_args: list[str] | None = None,
    ) -> WorkflowHandle:
        """Generate a parameterized fan-out Snakefile.

        Produces a one-rule Snakefile with expand() that scatters the tool
        over a collection. The container image (from conda_deps auto-derive
        or explicit container) is baked into the rule's container: directive.
        """
        container_image = resolve_container_from_spec(spec)

        # Use a unique temporary artifact path — submit() writes it to work_dir.
        # NamedTemporaryFile avoids the one-second-collision of time.time() names.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="bioledger_snakefile_", suffix=".smk", delete=False
        ) as tf:
            artifact_path = Path(tf.name)

        step_inventory, collection_inputs = _generate_snakefile(
            spec, input_mapping, container_image, artifact_path
        )

        output_patterns = build_output_patterns(spec)

        return WorkflowHandle(
            executor="snakemake",
            artifact_path=artifact_path,
            step_inventory=step_inventory,
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
        """Submit the generated Snakefile to snakemake.

        Binds actual input_mapping values via a config.yaml file, then
        launches snakemake as a subprocess.
        """
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Copy the Snakefile to the work dir
        snakefile_path = work_dir / "Snakefile"
        snakefile_path.write_text(handle.artifact_path.read_text())

        # Write config.yaml with bound values
        config_path = work_dir / "config.yaml"
        collection_inputs = handle.metadata.get("collection_inputs", [])
        config_path.write_text(
            self._build_config_yaml(input_mapping, params, collection_inputs)
        )

        # Build the snakemake command
        cmd = [
            self.executable,
            "-s", str(snakefile_path),
            "--configfile", str(config_path),
            "--stats", str(work_dir / "stats.json"),
            "--jobs", str(self._config.jobs),
        ]
        # Container runtime flag
        if handle.container_image and self._config.container_runtime != "none":
            cmd.append(f"--use-{self._config.container_runtime}")
        # Filter empty strings and add extra_args
        cmd = [c for c in cmd if c]
        extra_args = handle.metadata.get("extra_args", [])
        cmd.extend(extra_args)
        cmd.extend(self._config.extra_args)

        # Launch as subprocess (non-blocking) via the shared shell wrapper —
        # gets the .exit_code EXIT-trap sidecar for free.
        from bioledger.core.engines._subprocess import launch_logged_process

        return launch_logged_process(
            shlex.join(cmd),
            work_dir=work_dir,
            executor_name="snakemake",
            log_filename="snakemake.log",
            metadata={"engine_version": self._detect_version()},
        )

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Check if the snakemake subprocess is still running.

        ``.exit_code`` (written by the launch wrapper's EXIT trap) is the
        source of truth; ``_check_completion`` (stats.json + log grepping)
        is the fallback for runs without a sidecar.
        """
        from bioledger.core.engines._subprocess import poll_local_process

        return poll_local_process(
            run_handle,
            log_filename="snakemake.log",
            fallback_check=self._check_completion,
            progress_fn=_stats_progress,
        )

    def _check_completion(self, run_handle: RunHandle) -> RunStatus | None:
        """Determine completion status from log file and stats."""
        log_path = Path(run_handle.work_dir) / "snakemake.log"
        stats_path = Path(run_handle.work_dir) / "stats.json"

        # Check stats.json for exit codes
        if stats_path.exists():
            try:
                import json
                stats = json.loads(stats_path.read_text())
                total_stats = stats.get("total", {})
                exit_codes = total_stats.get("exit-code", {})
                total = total_stats.get("jobs", 0)
                succeeded = exit_codes.get("0", 0)
                failed = total - succeeded

                if failed > 0:
                    run_handle.status = "failed"
                    return RunStatus(
                        state="failed",
                        completed=succeeded,
                        total=total,
                        message=f"{failed}/{total} jobs failed",
                    )
                else:
                    run_handle.status = "completed"
                    return RunStatus(
                        state="completed",
                        completed=succeeded,
                        total=total,
                    )
            except (json.JSONDecodeError, KeyError):
                pass

        # Fall back to log file
        if log_path.exists():
            content = log_path.read_text()
            if "Error" in content or "error" in content:
                run_handle.status = "failed"
                return RunStatus(state="failed", message="See snakemake.log")
            if "Finished" in content or "nothing to be done" in content:
                run_handle.status = "completed"
                return RunStatus(state="completed")

        # Can't determine — unknown
        run_handle.status = "unknown"
        return RunStatus(state="unknown", message="Could not determine completion status")

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """Collect outputs and provenance from a completed snakemake run.

        Discovers output files from the work directory and references
        stats.json as a provenance log.
        """
        # Phase 2b will wire this to LedgerEntry enrichment.
        return []
