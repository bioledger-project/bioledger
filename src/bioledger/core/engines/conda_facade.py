"""CondaRunner facade implementing the ExecutionAdapter protocol.

Wraps ``CondaRunner`` so native conda single-tool runs can go through
``dispatch()``. Implements ``submit_spec()`` and ``poll()`` —
``generate()`` and ``submit()`` raise because conda cannot fan-out.
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

    ``submit_spec()`` delegates to ``executor.stage_and_submit()`` so that
    ``dispatch()`` → ``adapter.submit_spec()`` works uniformly for conda
    runs. The created ``LedgerEntry`` is carried in
    ``RunHandle.metadata["ledger_entry"]`` so the caller can attach it to
    the session.
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
        """Submit a single tool run via native conda.

        Delegates to ``executor.stage_and_submit()`` for staging, command
        rendering, conda env creation/launch, and ``LedgerEntry`` creation.
        The created entry is carried in ``RunHandle.metadata["ledger_entry"]``
        so the caller can attach it to the session.
        """
        from bioledger.forges.analysisforge.executor import (
            _spec_to_toolspec,
            stage_and_submit,
        )

        toolspec = _spec_to_toolspec(spec)
        input_files = {k: Path(v) for k, v in input_mapping.items()}
        # work_dir is the per-run dir; session_dir is its parent's parent
        # (sessions/<id>/runs/<run_id> → sessions/<id>)
        session_dir = work_dir.parent.parent
        entry, run_dir, _, _, _ = stage_and_submit(
            toolspec, input_files, session_dir, work_dir, params, None
        )
        return RunHandle(
            executor="conda",
            run_id=entry.run_handle_id or "",
            entry_id=entry.id,
            work_dir=run_dir,
            status="running",
            metadata={"ledger_entry": entry},
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
