"""Engine config validation — checks for container tech mismatches and common mistakes.

Called at dispatch time or via `bioledger config validate` to warn early about
configurations that will fail at runtime (e.g. containerized tools with no
container runtime enabled).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bioledger.core.engines.config import EnginesConfig

logger = logging.getLogger(__name__)


@dataclass
class ValidationWarning:
    """A single validation warning message."""

    engine: str
    message: str
    hint: str = ""


@dataclass
class ValidationResult:
    """Aggregated validation result."""

    warnings: list[ValidationWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.warnings) == 0

    def __iter__(self):
        return iter(self.warnings)


def validate_engine_config(
    config: EnginesConfig,
    specs: list[Any] | None = None,
) -> ValidationResult:
    """Validate engine configs for common mistakes and container tech mismatches.

    Args:
        config: The EnginesConfig to validate.
        specs: Optional list of ExecutionSpec objects to check for container
            mismatches (e.g. specs with containers but container_runtime=none).

    Returns:
        ValidationResult with warnings (no hard errors — config is always usable,
        just may produce confusing runtime failures).
    """
    result = ValidationResult()

    _check_executables(config, result)
    _check_config_files(config, result)
    _check_snakemake(config, result)
    _check_galaxy(config, result)

    if specs is not None:
        _check_container_mismatches(config, specs, result)

    return result


def _check_executables(
    config: EnginesConfig, result: ValidationResult,
) -> None:
    """Warn if an engine's executable is set but not found on PATH."""
    for engine_name in ("snakemake", "nextflow", "galaxy"):
        engine_config = getattr(config, engine_name, None)
        if engine_config is None:
            continue
        exe = getattr(engine_config, "executable", None)
        if exe is not None and not Path(exe).exists():
            if not shutil.which(exe):
                result.warnings.append(ValidationWarning(
                    engine=engine_name,
                    message=f"executable '{exe}' not found on PATH",
                    hint="Set executable to None for auto-detect, "
                         "or install the engine and update the path",
                ))


def _check_config_files(
    config: EnginesConfig, result: ValidationResult,
) -> None:
    """Warn if a config_file is set but doesn't exist."""
    for engine_name in ("snakemake", "nextflow", "galaxy"):
        engine_config = getattr(config, engine_name, None)
        if engine_config is None:
            continue
        cfg_file = getattr(engine_config, "config_file", None)
        if cfg_file is not None and not Path(cfg_file).exists():
            result.warnings.append(ValidationWarning(
                engine=engine_name,
                message=f"config_file '{cfg_file}' does not exist",
                hint="Create the config file or remove the setting",
            ))


def _check_snakemake(
    config: EnginesConfig, result: ValidationResult,
) -> None:
    """Snakemake-specific checks."""
    smk = config.snakemake
    if smk.jobs == 1 and smk.extra_args and any(
        "cluster" in arg for arg in smk.extra_args
    ):
        result.warnings.append(ValidationWarning(
            engine="snakemake",
            message="--cluster specified in extra_args but jobs=1 — "
                    "only one job will be submitted to the cluster at a time",
            hint="Set engines.snakemake.jobs > 1 for cluster parallelism",
        ))


def _check_galaxy(
    config: EnginesConfig, result: ValidationResult,
) -> None:
    """Galaxy-specific checks."""
    gxy = config.galaxy
    if gxy.mode == "planemo":
        result.warnings.append(ValidationWarning(
            engine="galaxy",
            message="mode is 'planemo' — this is for local development only",
            hint="For production/cluster use, set mode='remote' with "
                 "url and api_key",
        ))


def _check_container_mismatches(
    config: EnginesConfig,
    specs: list[Any],
    result: ValidationResult,
) -> None:
    """Check if specs with containers will work with the configured runtimes."""
    has_container_specs = any(
        getattr(s, "container", None) for s in specs
    )
    has_conda_specs = any(
        getattr(s, "conda_deps", None) for s in specs
    )

    if not has_container_specs and not has_conda_specs:
        return

    # Nextflow: warn if container specs exist but no config_file is set
    # (the generated crystallized config handles this, but for direct
    # nextflow runs the user needs their own config)
    if has_container_specs and config.nextflow.config_file is None:
        nf_extra = " ".join(config.nextflow.extra_args)
        if "-profile" not in nf_extra:
            result.warnings.append(ValidationWarning(
                engine="nextflow",
                message="specs have containers but no config_file or "
                        "-profile in extra_args — containers may not be "
                        "enabled at runtime",
                hint="Set engines.nextflow.config_file to a nextflow.config "
                     "with docker/singularity enabled, or add "
                     "extra_args=['-profile', 'docker']",
            ))

    # Snakemake: warn if container specs exist but runtime is 'none'
    if has_container_specs and config.snakemake.container_runtime == "none":
        result.warnings.append(ValidationWarning(
            engine="snakemake",
            message="specs have containers but container_runtime='none' — "
                    "tools will run without container isolation",
            hint="Set engines.snakemake.container_runtime to 'apptainer' "
                 "or 'singularity'",
        ))

    # Snakemake: warn if conda specs exist but runtime is not 'conda'
    if has_conda_specs and config.snakemake.container_runtime not in ("conda", "none"):
        result.warnings.append(ValidationWarning(
            engine="snakemake",
            message="specs have conda_deps but container_runtime is "
                    f"'{config.snakemake.container_runtime}' — conda "
                    "environments will not be used",
            hint="Set engines.snakemake.container_runtime to 'conda' "
                 "for conda-based execution",
        ))


def format_warnings(result: ValidationResult) -> str:
    """Format validation warnings as a human-readable string."""
    if result.ok:
        return "Engine config: OK"
    lines = [f"Engine config: {len(result.warnings)} warning(s)"]
    for w in result.warnings:
        lines.append(f"  [{w.engine}] {w.message}")
        if w.hint:
            lines.append(f"    -> {w.hint}")
    return "\n".join(lines)
