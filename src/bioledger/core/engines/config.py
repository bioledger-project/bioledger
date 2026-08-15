"""Engine configuration models for BioLedgerConfig.

Passthrough-only design — no cross-engine abstract fields. Each engine has
``executable``, ``config_file``, and ``extra_args``. Galaxy is the only engine
with additional fields (``mode``, ``url``, ``api_key``).

These types are defined here (not in the config module) to avoid inverted
dependencies — ``agent.py`` and ``ledger/models.py`` both need ``EngineName``
without depending on the config module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

EngineName = Literal["nextflow", "snakemake", "galaxy"]
"""Which workflow engine? Narrow set: only engines that can do fan-out,
scheduling, native scatter. Used on ToolRunRequest.engine and
EnginesConfig.default. Does NOT include "docker" or "conda" — those are
executors, not selectable engines."""


class EngineConfig(BaseModel):
    """Base per-engine config. Passthrough-only — no cross-engine abstractions."""

    executable: str | None = None
    # Path to engine binary. None = auto-detect (pip extra, then PATH).

    config_file: Path | None = None
    # Path to engine-native config file (nextflow.config, profiles, etc.).
    # Passed to the engine as-is. We never parse or generate engine config.

    extra_args: list[str] = Field(default_factory=list)
    # Raw CLI flags passed through verbatim at submit time.


class GalaxyEngineConfig(EngineConfig):
    """Galaxy-specific config. Only engine with fields beyond the base."""

    mode: Literal["planemo", "remote"] = "planemo"
    # planemo: local Galaxy instance via planemo (pip-installable).
    # remote: connect to an existing Galaxy server via bioblend.

    url: str = ""
    # Galaxy server URL, e.g. "https://usegalaxy.org". Required when mode == "remote".

    api_key: str | None = None
    # Required when mode == "remote". None → validation error.
    # "" → deliberate anonymous-access opt-out (explicit, not silently permissive).
    # Non-empty string → real API key.
    # Also settable via BIOLEDGER_GALAXY_API_KEY env var (takes precedence).

    @model_validator(mode="after")
    def _validate_remote_mode(self) -> "GalaxyEngineConfig":
        if self.mode == "remote":
            if not self.url:
                raise ValueError(
                    "galaxy.url is required when galaxy.mode == 'remote'"
                )
            if self.api_key is None:
                raise ValueError(
                    "galaxy.api_key is required when galaxy.mode == 'remote' "
                    "(set to empty string explicitly for anonymous access)"
                )
        return self


class SnakemakeEngineConfig(EngineConfig):
    """Snakemake config."""

    container_runtime: Literal["apptainer", "singularity", "conda", "none"] = "apptainer"
    # Which container runtime to use for snakemake's --use-<runtime> flag.
    # "apptainer" (default) → --use-apptainer
    # "singularity" → --use-singularity
    # "conda" → --use-conda
    # "none" → no flag (run natively)

    jobs: int = 1
    # Number of jobs to run in parallel. Passed as --jobs to snakemake.
    # Set to >1 for local parallelism, or combine with extra_args for cluster.


class NextflowEngineConfig(EngineConfig):
    """Nextflow config. No fields beyond base."""

    pass


class EnginesConfig(BaseModel):
    """Top-level engines section in BioLedgerConfig."""

    default: Literal["auto"] | EngineName = "auto"
    # "auto" = auto-detect chain (scan for installed engines in preference
    # order: nextflow > snakemake > galaxy > error with install options).
    # Or explicit engine name: "nextflow", "snakemake", "galaxy".

    snakemake: SnakemakeEngineConfig = Field(default_factory=SnakemakeEngineConfig)
    nextflow: NextflowEngineConfig = Field(default_factory=NextflowEngineConfig)
    galaxy: GalaxyEngineConfig = Field(default_factory=GalaxyEngineConfig)
