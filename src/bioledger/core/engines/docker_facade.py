"""DockerRunner facade implementing the ExecutionAdapter protocol.

Wraps the existing ``DockerRunner`` so single-tool Docker runs can go through
the same ``dispatch()`` path as engine-submitted runs. Implements
``submit_spec()`` and ``poll()`` — ``generate()`` raises NotImplementedError
because Docker cannot fan-out (fan-out requires a real workflow engine).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bioledger.core.containers.docker import DockerRunner
from bioledger.core.engines.protocol import RunHandle, RunStatus, WorkflowHandle


class DockerRunnerFacade:
    """Adapter wrapping DockerRunner for the ExecutionAdapter protocol.

    This is the adapter for ``executor="docker"``. It delegates to the
    existing DockerRunner, which handles the actual container lifecycle.
    Native conda execution (``executor="conda"``) is handled by
    ``CondaRunnerFacade``.

    ``submit_spec()`` delegates to ``executor.stage_and_submit()`` so that
    ``dispatch()`` → ``adapter.submit_spec()`` works uniformly for Docker
    runs. The created ``LedgerEntry`` is carried in
    ``RunHandle.metadata["ledger_entry"]`` so the caller can attach it to
    the session.
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
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Submit a single tool run via Docker.

        Delegates to ``executor.stage_and_submit()`` for staging, command
        rendering, container launch, and ``LedgerEntry`` creation. The
        created entry is carried in ``RunHandle.metadata["ledger_entry"]``
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
            executor="docker",
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
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        raise NotImplementedError(
            "Docker cannot submit generated workflows — use submit_spec() for single tools."
        )

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Poll a Docker container for completion."""
        result = self.runner.poll(run_handle.run_id)
        if result is None:
            return None
        run_handle.status = "completed" if result.exit_code == 0 else "failed"
        return RunStatus(
            state="completed" if result.exit_code == 0 else "failed",
            message=result.stderr or "",
        )

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """Collect outputs from a completed Docker run.

        Docker runs don't produce engine-native provenance files — the
        ledger entry itself is the provenance record. For the Docker facade,
        poll() and collect() share the same underlying poll_tool() call,
        since Docker's poll_tool() already does both status-check and
        finalization in one step. collect() after a terminal poll() is
        effectively a no-op for Docker.
        """
        return []
