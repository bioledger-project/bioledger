"""ExecutionAdapter protocol and supporting types.

Defines the interface that all engine adapters (Docker facade, Snakemake,
Nextflow, Galaxy) implement. The protocol has five methods:

- ``submit_spec()``: single-item dispatch — run one tool from its toolspec.
- ``generate()``: fan-out — produce a parameterized workflow (NF/Snakefile/.ga)
  that scatters the tool over a collection.
- ``submit()``: submit a generated workflow to the engine, return a run handle.
- ``poll()``: check if a submitted run is done; return RunStatus if terminal.
- ``collect()``: enrich the LedgerEntry with outputs + provenance from a completed run.

``WorkflowHandle`` and ``RunHandle`` are pydantic models for managing
workflow and run states across the submit/poll lifecycle. Both serialize
to/from JSON directly so they survive process restarts alongside the
``LedgerEntry`` that references them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class RunStatus(BaseModel):
    """Status of a submitted run, returned by ``poll()``.

    Carries progress info for fan-out runs (completed/total counts)
    and an engine-native status message.
    """

    state: Literal["pending", "queued", "running", "completed", "failed", "unknown"]
    completed: int = 0  # number of completed tasks (fan-out progress)
    total: int = 0  # total tasks in the run (0 if unknown)
    message: str = ""  # engine-native status message or error text


class WorkflowHandle(BaseModel):
    """Represents a generated but not-yet-submitted workflow.

    Created by ``generate()``, consumed by ``submit()``.
    Only produced by ``generate()`` for fan-out — ``submit_spec()`` does not
    produce a WorkflowHandle (the toolspec itself is the spec).
    """

    executor: str  # engine adapter that generated this (nextflow/snakemake/galaxy)
    artifact_path: Path  # generated main.nf / Snakefile / .ga
    step_inventory: list[str] = Field(default_factory=list)  # process/rule names
    params_schema: dict[str, Any] = Field(default_factory=dict)  # workflow-level params
    container_image: str = ""  # resolved container image (from spec or auto-derive)
    input_mapping: dict[str, str] = Field(default_factory=dict)  # slot → collection spec
    output_patterns: list[str] = Field(default_factory=list)  # expected output globs
    metadata: dict[str, Any] = Field(default_factory=dict)  # engine-specific extras


class RunHandle(BaseModel):
    """Represents a submitted run being polled for completion.

    Created by ``submit()`` or ``submit_spec()``, consumed by ``poll()``
    and ``collect()``. Serializes to JSON for restart resilience.
    """

    executor: str  # "docker" for DockerRunner facade, or engine name
    run_id: str  # Docker container ID, NF run name, snakemake job ID, etc.
    entry_id: str  # ID of the LedgerEntry created at submit time
    work_dir: Path  # where the run executes
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "running"  # "running", "completed", "failed"


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Interface for all execution backends.

    Implemented by:
    - ``DockerRunnerFacade``: wraps the existing DockerRunner for single-tool
      runs. ``submit_spec()``/``poll()``/``collect()`` are functional;
      ``generate()``/``submit()`` raise NotImplementedError (Docker can't fan-out).
    - ``SnakemakeAdapter`` (Phase 2): full implementation.
    - ``NextflowAdapter`` (Phase 5): full implementation.
    - ``GalaxyAdapter`` (Phase 5): full implementation.
    """

    def submit_spec(
        self,
        spec: Any,  # ExecutionSpec
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Run a single tool from its toolspec.

        Creates a LedgerEntry immediately (run_status="running").
        For workflow toolspecs with engine executors, the engine scatters
        natively over collection inputs. For Docker/conda toolspecs, this
        wraps the existing submit_tool() path.
        """
        ...

    def generate(
        self,
        spec: Any,  # ExecutionSpec
        input_mapping: dict[str, str],
        extra_args: list[str] | None = None,
    ) -> WorkflowHandle:
        """Generate a parameterized fan-out workflow for this engine.

        The workflow scatters the tool over a collection (detected from
        ToolInput.collection=True on inputs). The returned WorkflowHandle
        is passed to ``submit()`` to bind actual values and launch.
        """
        ...

    def submit(
        self,
        handle: WorkflowHandle,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        work_dir: Path,
    ) -> RunHandle:
        """Submit a generated workflow to the engine.

        Binds actual input_mapping values to the parameterized workflow
        and launches it. Creates a LedgerEntry (run_status="running").
        Returns a RunHandle for polling.
        """
        ...

    def poll(self, run_handle: RunHandle) -> RunStatus | None:
        """Check if a submitted run is complete.

        Returns None if the run ID is not found (lost/never existed).
        Returns RunStatus with a terminal state when the run is done.
        """
        ...

    def collect(self, run_handle: RunHandle) -> list[Any]:
        """Enrich the LedgerEntry created at submit time with outputs
        and provenance from a completed run.

        Returns a list with the enriched LedgerEntry (one entry per
        submission, whether single-tool or fan-out). Call after poll()
        returns a terminal state; idempotent.
        """
        ...
