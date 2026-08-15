"""CondaRunner facade implementing the ExecutionAdapter protocol.

Wraps ``CondaRunner`` so native conda single-tool runs can go through
``dispatch()``. Only ``poll()`` is functional — ``submit_spec()`` raises
``NotImplementedError`` because the actual submission still lives in
``executor.py`` (same pattern as ``DockerRunnerFacade``). ``generate()``
and ``submit()`` raise because conda cannot fan-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bioledger.core.conda_runner import CondaRunner
from bioledger.core.engines.protocol import RunHandle, RunStatus, WorkflowHandle


class CondaRunnerFacade:
    """Adapter wrapping CondaRunner for the ExecutionAdapter protocol.

    This is the adapter for ``executor="conda"`` when conda runs natively
    (via ``conda run``) rather than being auto-derived to a container image.
    It delegates to ``CondaRunner``, which handles conda env creation,
    process launch, and polling.

    Note: ``submit_spec()`` here is a thin stub — the existing executor.py
    functions (``run_tool``, ``submit_tool``, ``poll_tool``) already handle
    staging, command rendering, and entry creation. This facade exists so
    ``dispatch()`` has a uniform interface; the heavy lifting stays in
    executor.py for now. Phase 2b will fold ``check_job_status`` /
    ``poll_pending_jobs`` to call ``adapter.poll()`` uniformly.
    """

    def __init__(self, runner: CondaRunner | None = None) -> None:
        self._runner = runner

    @property
    def runner(self) -> CondaRunner:
        if self._runner is None:
            self._runner = CondaRunner()
        return self._runner

    def submit_spec(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        raise NotImplementedError(
            "CondaRunnerFacade.submit_spec is not yet wired — "
            "Conda runs still go through executor.py directly. "
            "This will be connected in Phase 2b when dispatch() is unified."
        )

    def generate(
        self,
        spec: Any,
        input_mapping: dict[str, str],
        extra_args: list[str] | None = None,
    ) -> WorkflowHandle:
        raise NotImplementedError(
            "Conda cannot fan-out. Fan-out requires a workflow engine "
            "(nextflow/snakemake/galaxy). Install one with: "
            "pip install bioledger[snakemake] or bioledger[galaxy]."
        )

    def submit(
        self,
        handle: WorkflowHandle,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        raise NotImplementedError(
            "Conda cannot submit generated workflows — use submit_spec() for single tools."
        )

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Poll a conda process for completion."""
        result = self.runner.poll(
            run_handle.run_id,
            run_handle.work_dir,
            expected_start_time=run_handle.process_start_time,
        )
        if result is None:
            return None
        run_handle.status = "completed" if result.exit_code == 0 else "failed"
        return RunStatus(
            state="completed" if result.exit_code == 0 else "failed",
            message=result.stderr or "",
        )

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """Collect outputs from a completed conda run.

        Same as Docker — the ledger entry itself is the provenance record.
        collect() after a terminal poll() is effectively a no-op.
        """
        return []
