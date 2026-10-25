"""Galaxy adapter — single-tool runs via the toolspec's templated command.

Implements the ``ExecutionAdapter`` protocol:

- ``submit_spec()``: Jinja-render ``spec.command`` (a ``planemo run ...``
  or equivalent invocation) and launch it as a local subprocess.
- ``poll()``: PID/``.exit_code``-based completion check via the shared
  ``poll_local_process`` machinery.
- ``generate()`` / ``submit()``: workflow fan-out is not supported for
  Galaxy — raises ``NotImplementedError`` (Snakemake/Nextflow fan-out
  remains supported).
- ``collect()``: no-op — outputs are discovered from the run directory
  by the generic file-discovery path.

The earlier planemo-serve/bioblend workflow machinery was removed: it
was unreachable (``generate()`` has always raised) and its polling used
bioblend invocation IDs that ``submit_spec()`` never produced.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from bioledger.core.engines.conda_derive import resolve_container_from_spec
from bioledger.core.engines.config import GalaxyEngineConfig
from bioledger.core.engines.protocol import (
    RunHandle,
    RunStatus,
    WorkflowHandle,
)

logger = logging.getLogger(__name__)


def _check_planemo_available() -> str:
    """Return the planemo executable path, or raise with install instructions."""
    exe = shutil.which("planemo")
    if exe is None:
        raise RuntimeError(
            "planemo is not installed. Install with: pip install bioledger[galaxy]"
        )
    return exe


def _resolve_container(spec: Any) -> str:
    """Resolve the container image from spec.container or spec.conda_deps.

    .. deprecated:: Use ``resolve_container_from_spec`` from
        ``conda_derive`` directly.
    """
    return resolve_container_from_spec(spec)


class GalaxyAdapter:
    """Galaxy adapter for single-tool runs discovered from the Tool Shed.

    Runs the toolspec's rendered ``command`` as a local subprocess —
    typically a ``planemo run ...`` invocation against a local Galaxy.
    Workflow fan-out (generate/submit) is not supported.
    """

    _engine_name = "galaxy"

    def __init__(self, config: GalaxyEngineConfig | None = None) -> None:
        self._config = config or GalaxyEngineConfig()

    @property
    def executable(self) -> str:
        if self._config.executable:
            return self._config.executable
        return _check_planemo_available()

    def _detect_version(self) -> str:
        """Detect the planemo version by shelling out once; cached."""
        if hasattr(self, "_cached_version"):
            return self._cached_version
        from bioledger.core.engines.versions import detect_engine_version
        try:
            exe = self.executable
        except RuntimeError:
            self._cached_version = "galaxy unknown"
            return self._cached_version
        self._cached_version = detect_engine_version(self._engine_name, exe)
        return self._cached_version

    def submit_spec(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Run a single workflow toolspec via Galaxy.

        Jinja-renders ``spec.command`` with real input paths and parameter
        values, then launches the rendered command as a subprocess. The
        command is expected to be a valid ``planemo run ...`` invocation.
        """
        from bioledger.core.engines._subprocess import render_and_launch

        command_template = getattr(spec, "command", "")
        if not command_template:
            raise RuntimeError(
                "Galaxy spec has no command field — cannot run directly. "
                "Provide a valid 'planemo run ...' invocation in the command field."
            )

        return render_and_launch(
            command_template=command_template,
            input_mapping=input_mapping,
            params=params,
            work_dir=work_dir,
            executor_name="galaxy",
            engine_version=self._detect_version(),
            log_filename="galaxy.log",
            spec=spec,
        )

    def generate(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        extra_args: list[str] | None = None,
    ) -> WorkflowHandle:
        """Galaxy fan-out is not supported.

        Use Snakemake or Nextflow for fan-out workflows. Galaxy remains
        supported for single-tool runs and Tool Shed discovery via
        ``submit_spec()``.
        """
        raise NotImplementedError(
            "Galaxy fan-out is not yet supported. Fan-out requires "
            "Snakemake or Nextflow. Install one with: "
            "pip install bioledger[snakemake] or "
            "pip install bioledger[nextflow]. Galaxy is supported for "
            "single-tool runs and Tool Shed discovery."
        )

    def submit(
        self,
        handle: WorkflowHandle,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Galaxy fan-out submission is not supported (see generate())."""
        raise NotImplementedError(
            "Galaxy fan-out is not yet supported — generate() cannot "
            "produce a workflow, so there is nothing to submit."
        )

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Check if a submitted run is still running.

        ``submit_spec()`` runs are local subprocesses — poll via the
        shared PID/``.exit_code`` path with a ``galaxy.log`` fallback.
        """
        from bioledger.core.engines._subprocess import (
            log_fallback,
            poll_local_process,
        )

        return poll_local_process(
            run_handle,
            log_filename="galaxy.log",
            fallback_check=log_fallback("galaxy.log"),
        )

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """No remote collection — outputs come from the run directory.

        ``_finalize_engine_entry`` calls this inside try/except; local
        subprocess runs produce their outputs on disk, discovered by the
        generic output-sweep path.
        """
        return []
