"""ExecutionAdapter protocol and supporting types.

Defines the interface that all engine adapters (Docker facade, Snakemake,
Nextflow, Galaxy) implement. The protocol has five methods:

- ``submit_spec()``: single-item dispatch — run one tool from its toolspec.
- ``generate()``: fan-out — produce a parameterized workflow (NF/Snakefile/.ga)
  that scatters the tool over a collection.
- ``submit()``: submit a generated workflow to the engine, return a run handle.
- ``poll()``: check if a submitted run is done; return results if terminal.
- ``collect()``: gather outputs + provenance from a completed run.

``WorkflowHandle`` and ``RunHandle`` are pydantic models for managing
workflow and run states across the submit/poll lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from bioledger.core.engines.config import EngineName


@dataclass
class RunResult:
    """Result of a completed run (single-tool or fan-out)."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    output_files: list[Path] = field(default_factory=list)
    # Engine-native provenance artifact paths (trace, stats, invocations, etc.)
    provenance_files: list[Path] = field(default_factory=list)


class WorkflowHandle(BaseModel):
    """Represents a generated but not-yet-submitted workflow.

    Created by ``generate()``, consumed by ``submit()``.
    """

    engine: EngineName
    workflow_text: str  # generated NF/Snakefile/.ga content
    workflow_path: Path | None = None  # where it was written (if applicable)
    container_image: str = ""  # resolved container image (from spec or auto-derive)
    input_mapping: dict[str, str] = Field(default_factory=dict)
    # slot → filename or collection spec
    output_patterns: list[str] = Field(default_factory=list)
    # expected output glob patterns
    metadata: dict[str, Any] = Field(default_factory=dict)
    # engine-specific extras (scatter axis, params, etc.)


class RunHandle(BaseModel):
    """Represents a submitted run being polled for completion.

    Created by ``submit()``, consumed by ``poll()`` and ``collect()``.
    """

    engine: EngineName
    run_id: str  # engine-native run identifier (NF run name, snakemake PID, etc.)
    workflow_handle: WorkflowHandle
    work_dir: Path  # where the run executes
    submit_time: str = ""  # ISO timestamp
    status: str = "running"  # "running", "completed", "failed"


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Interface for all execution backends.

    Implemented by:
    - ``DockerRunnerFacade``: wraps the existing DockerRunner for single-tool
      runs. Only implements ``submit_spec()`` and ``poll()``; ``generate()``
      raises NotImplementedError (Docker can't fan-out).
    - ``SnakemakeAdapter`` (Phase 2): full implementation.
    - ``NextflowAdapter`` (Phase 5): full implementation.
    - ``GalaxyAdapter`` (Phase 5): full implementation.
    """

    def submit_spec(
        self,
        spec: Any,  # ExecutionSpec
        input_files: dict[str, Path],
        work_dir: Path,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, RunResult | None]:
        """Run a single tool from its toolspec.

        Returns (run_handle_id, RunResult | None).
        If RunResult is not None, the run completed synchronously (blocking mode).
        If RunResult is None, the run is async — call poll() with the handle ID.
        """
        ...

    def generate(
        self,
        spec: Any,  # ExecutionSpec
        input_mapping: dict[str, str],
        work_dir: Path,
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
        work_dir: Path,
    ) -> RunHandle:
        """Submit a generated workflow to the engine.

        Binds actual input_mapping values to the parameterized workflow
        and launches it. Returns a RunHandle for polling.
        """
        ...

    def poll(self, run_handle: RunHandle) -> RunResult | None:
        """Check if a submitted run is complete.

        Returns None if still running, RunResult if terminal.
        """
        ...

    def collect(self, run_handle: RunHandle) -> tuple[list[Path], dict[str, Any]]:
        """Collect outputs and provenance from a completed run.

        Returns (output_files, provenance_dict). The provenance_dict
        contains engine-native artifacts normalized to a common shape.
        """
        ...
