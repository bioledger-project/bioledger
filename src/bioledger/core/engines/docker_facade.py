"""DockerRunner facade implementing the ExecutionAdapter protocol.

Wraps the existing ``DockerRunner`` so single-tool Docker runs can go through
the same ``dispatch()`` path as engine-submitted runs. Only implements
``submit_spec()`` and ``poll()`` — ``generate()`` raises NotImplementedError
because Docker cannot fan-out (fan-out requires a real workflow engine).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bioledger.core.containers.docker import DockerRunner, RunResult
from bioledger.core.engines.protocol import RunHandle, WorkflowHandle


class DockerRunnerFacade:
    """Adapter wrapping DockerRunner for the ExecutionAdapter protocol.

    This is the adapter for ``executor="docker"`` and ``executor="conda"``
    (conda resolves to a container image via auto-derive, then runs in Docker).
    It delegates to the existing DockerRunner, which handles the actual
    container lifecycle.

    Note: ``submit_spec()`` here is a thin wrapper — the existing executor.py
    functions (``run_tool``, ``submit_tool``, ``poll_tool``) already handle
    staging, command rendering, and entry creation. This facade exists so
    ``dispatch()`` has a uniform interface; the heavy lifting stays in
    executor.py for now. Phase 2b will fold ``check_job_status`` /
    ``poll_pending_jobs`` to call ``adapter.poll()`` uniformly.
    """

    def __init__(self, runner: DockerRunner | None = None) -> None:
        self._runner = runner

    @property
    def runner(self) -> DockerRunner:
        if self._runner is None:
            self._runner = DockerRunner()
        return self._runner

    def submit_spec(
        self,
        spec: Any,
        input_files: dict[str, Path],
        work_dir: Path,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, RunResult | None]:
        """Submit a single tool run via Docker.

        This is a synchronous stub — the actual staging and command rendering
        live in executor.py's ``_submit_and_stage``. This facade returns
        a placeholder handle; the real submission is done by executor.py.

        For the Phase 1 protocol surface, this method exists to satisfy the
        protocol. The dispatch path for Docker runs still goes through
        executor.py directly; Phase 2b will route through here.
        """
        raise NotImplementedError(
            "DockerRunnerFacade.submit_spec is not yet wired — "
            "Docker runs still go through executor.py directly. "
            "This will be connected in Phase 2b when dispatch() is unified."
        )

    def generate(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        work_dir: Path,
    ) -> WorkflowHandle:
        """Docker cannot fan-out — requires a real workflow engine."""
        raise NotImplementedError(
            "Docker cannot fan-out. Fan-out requires a workflow engine "
            "(nextflow/snakemake/galaxy). Install one with: "
            "pip install bioledger[snakemake] or bioledger[galaxy]."
        )

    def submit(
        self,
        handle: WorkflowHandle,
        input_mapping: dict[str, str],
        work_dir: Path,
    ) -> RunHandle:
        raise NotImplementedError(
            "Docker cannot submit generated workflows — use submit_spec() for single tools."
        )

    def poll(self, run_handle: RunHandle) -> RunResult | None:
        """Poll a Docker container for completion."""
        result = self.runner.poll(run_handle.run_id)
        if result is not None:
            run_handle.status = "completed" if result.exit_code == 0 else "failed"
        return result

    def collect(self, run_handle: RunHandle) -> tuple[list[Path], dict[str, Any]]:
        """Collect outputs from a completed Docker run.

        Docker runs don't produce engine-native provenance files — the
        ledger entry itself is the provenance record.
        """
        return ([], {})
