"""Dispatch function: routes tool run requests to the correct adapter.

``dispatch()`` is the single entry point for executing a tool. It inspects
the toolspec's executors and the optional ``engine`` override, then selects
the appropriate ``ExecutionAdapter``:

- ``docker`` executor → ``DockerRunnerFacade`` (delegates to executor.py staging)
- ``conda`` executor → ``CondaRunnerFacade`` (native conda execution)
- ``nextflow`` / ``snakemake`` / ``galaxy`` executors → engine adapter
  (looked up by engine name; all three are implemented)

If no engine is specified for a fan-out request, auto-detect runs (config
default → scan for installed engines → error with install options).
"""

from __future__ import annotations

import logging
import shutil
from typing import Any

from bioledger.core.engines.config import EngineName, EnginesConfig
from bioledger.core.engines.conda_facade import CondaRunnerFacade
from bioledger.core.engines.docker_facade import DockerRunnerFacade
from bioledger.core.engines.protocol import ExecutionAdapter
from bioledger.core.engines.validate import format_warnings, validate_engine_config

logger = logging.getLogger(__name__)

# Registry of engine adapter factories, keyed by engine name.
_adapter_registry: dict[str, type] = {}


def register_adapter(engine: str, adapter_cls: type) -> None:
    """Register an adapter class for an engine name."""
    _adapter_registry[engine] = adapter_cls


# Register the Snakemake adapter (import-safe — snakemake_adapter imports
# snakemake lazily, so this works even without the extra installed).
try:
    from bioledger.core.engines.snakemake_adapter import SnakemakeAdapter
    register_adapter("snakemake", SnakemakeAdapter)
except ImportError:
    pass

# Register the Nextflow adapter (import-safe — nextflow_adapter shells out
# to the nextflow binary lazily, so this works even without nextflow installed).
try:
    from bioledger.core.engines.nextflow_adapter import NextflowAdapter
    register_adapter("nextflow", NextflowAdapter)
except ImportError:
    pass

# Register the Galaxy adapter (import-safe — galaxy_adapter imports bioblend
# lazily, so this works even without the extra installed).
try:
    from bioledger.core.engines.galaxy_adapter import GalaxyAdapter
    register_adapter("galaxy", GalaxyAdapter)
except ImportError:
    pass


def _detect_installed_engine() -> str | None:
    """Auto-detect the first available engine in preference order.

    Preference: nextflow > snakemake > galaxy.
    Returns the engine name, or None if none found.
    """
    for engine in ("nextflow", "snakemake", "galaxy"):
        if shutil.which(engine) is not None:
            return engine
        # Also check if the pip extra is installed
        try:
            if engine == "snakemake":
                import snakemake  # noqa: F401
                return engine
            elif engine == "nextflow":
                # nextflow is a Java binary, not a Python package
                pass
            elif engine == "galaxy":
                import galaxy.tool_util  # noqa: F401
                return engine
        except ImportError:
            pass
    return None


def _get_engine_adapter(
    engine: EngineName,
    config: EnginesConfig | None = None,
) -> ExecutionAdapter:
    """Get or create the adapter for the specified engine.

    Raises NotImplementedError with install instructions if the adapter
    is not yet implemented.
    """
    if engine not in _adapter_registry:
        raise NotImplementedError(
            f"{engine} adapter not yet implemented. "
            f"Available: {', '.join(_adapter_registry.keys()) or 'none yet'}. "
            f"Install with: pip install bioledger[{engine}]"
        )

    adapter_cls = _adapter_registry[engine]
    # Adapters are instantiated fresh each call — they hold no mutable state
    # beyond the config, which is passed through.
    if config is not None:
        engine_config = getattr(config, engine, None)
        if engine_config is not None:
            return adapter_cls(engine_config)
    return adapter_cls()


def dispatch(
    spec: Any,  # ExecutionSpec
    engine: EngineName | None = None,
    config: EnginesConfig | None = None,
) -> ExecutionAdapter:
    """Select the correct adapter for a tool run request.

    Inspects the toolspec's executors and the optional ``engine`` override
    to determine which adapter to use.

    Args:
        spec: The ExecutionSpec to run.
        engine: Optional engine override (nextflow/snakemake/galaxy).
            If None, auto-detects based on the spec's executors.
        config: Optional EnginesConfig for adapter construction.

    Returns:
        An ExecutionAdapter instance ready to call submit_spec() or
        generate() + submit() on.

    Raises:
        NotImplementedError: If the selected engine adapter is not yet
            implemented, with install instructions.
        ValueError: If the spec has no usable executors or the engine
            override conflicts with the spec's executors.
    """
    executors = getattr(spec, "executors", ["docker"])
    engine_executors = [e for e in executors if e in ("nextflow", "snakemake", "galaxy")]

    # Validate engine config for container tech mismatches
    if config is not None and engine_executors:
        vr = validate_engine_config(config, [spec])
        if not vr.ok:
            for w in vr:
                logger.warning("[%s] %s", w.engine, w.message)
            logger.debug(format_warnings(vr))

    # If an engine is explicitly requested, use it
    if engine is not None:
        if engine not in executors and engine not in engine_executors:
            # Allow override even if not in spec.executors — the engine
            # runs the tool regardless of declared executors
            logger.warning(
                "Engine %s requested but not in spec.executors=%s — "
                "proceeding anyway", engine, executors
            )
        return _get_engine_adapter(engine, config)

    # No engine override — check if the spec has engine executors
    if engine_executors:
        if len(engine_executors) > 1:
            raise ValueError(
                f"Spec has multiple engine executors ({engine_executors}) — "
                f"specify engine= to disambiguate"
            )
        return _get_engine_adapter(engine_executors[0], config)

    # No engine executor — use Docker or Conda facade
    if "docker" in executors:
        return DockerRunnerFacade()
    if "conda" in executors:
        return CondaRunnerFacade()

    raise ValueError(
        f"No usable executors in spec.executors={executors} "
        f"and no engine override specified"
    )


def resolve_engine_for_fanout(
    config: EnginesConfig | None = None,
    engine: EngineName | None = None,
) -> str:
    """Determine which engine to use for a fan-out request.

    Fan-out requires a real workflow engine — there is no Docker fallback.

    Args:
        config: EnginesConfig with default setting.
        engine: Optional explicit engine override.

    Returns:
        Engine name to use.

    Raises:
        RuntimeError: If no engine is available, with install instructions.
        NotImplementedError: If Galaxy is selected for fan-out (not yet supported).
    """
    if engine is not None:
        resolved = engine
    elif config is not None and config.default != "auto":
        resolved = config.default
    else:
        # Auto-detect
        detected = _detect_installed_engine()
        if detected is None:
            raise RuntimeError(
                "Fan-out requires a workflow engine, but none were detected. "
                "Install one with: pip install bioledger[snakemake] "
                "or pip install bioledger[nextflow]"
            )
        resolved = detected

    if resolved == "galaxy":
        raise NotImplementedError(
            "Galaxy fan-out is not yet supported. Fan-out requires Snakemake or Nextflow. "
            "Install one with: pip install bioledger[snakemake] or pip install bioledger[nextflow]. "
            "Galaxy is supported for single-tool runs and Tool Shed discovery."
        )
    return resolved
