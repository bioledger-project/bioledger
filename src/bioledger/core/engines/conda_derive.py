"""Conda dependency → container image auto-derivation.

Resolves ``conda_deps`` (a list of conda package specs like ``["samtools=1.17"]``)
to a fully-qualified container image URL, so that toolspecs using ``conda_deps``
instead of an explicit ``container`` field can be used for fan-out via
``generate()``.

Two strategies:
- **Single-package tools**: use ``lookup_biocontainers_tag`` from
  ``bioledger_toolspec_schema.containers`` to find a BioContainers image tag.
- **Multi-package toolspecs**: use ``galaxy-tool-util``'s ``v2_image_name``
  to compute a deterministic mulled-hash container image.

The resolved image is recorded on the ``LedgerEntry`` for provenance and baked
into generated workflow process/rule text at ``generate()`` time.
"""

from __future__ import annotations

import logging
from typing import Any

from bioledger_toolspec_schema.containers import lookup_biocontainers_tag_sync

logger = logging.getLogger(__name__)


def resolve_container_from_spec(spec: Any) -> str:
    """Resolve the container image from a spec's container or conda_deps.

    If ``spec.container`` is set, returns it directly. Otherwise, tries
    to auto-derive from ``spec.conda_deps`` via ``derive_container_image``.

    Returns "" if no container can be resolved.
    """
    container = getattr(spec, "container", "")
    if container:
        return container
    conda_deps = getattr(spec, "conda_deps", [])
    if conda_deps:
        image = derive_container_image(conda_deps)
        if image:
            return image
    return ""


def _parse_package_spec(spec: str) -> tuple[str, str]:
    """Parse a conda package spec like 'samtools=1.17' into (name, version).

    Returns (name, "") if no version is specified.
    """
    if "=" in spec:
        name, version = spec.split("=", 1)
        return name.strip(), version.strip()
    return spec.strip(), ""


def derive_container_image(conda_deps: list[str]) -> str | None:
    """Resolve a list of conda package specs to a container image URL.

    For a single package, uses BioContainers tag lookup (network call).
    For multiple packages, uses galaxy-tool-util's mulled-hash mechanism
    (deterministic, no network call for the hash itself).

    Args:
        conda_deps: List of conda package specs (e.g. ``["samtools=1.17"]``).

    Returns:
        Fully-qualified container image URL (e.g.
        ``"quay.io/biocontainers/samtools:1.17--hdfd78af_0"``) or ``None``
        if resolution fails.
    """
    if not conda_deps:
        return None

    if len(conda_deps) == 1:
        pkg, version = _parse_package_spec(conda_deps[0])
        image = lookup_biocontainers_tag_sync(pkg, version)
        if image:
            return image
        logger.warning(
            "BioContainers lookup failed for %s=%s; falling back to mulled hash",
            pkg, version,
        )
        # Fall through to mulled-hash for single packages too — v2_image_name
        # handles single targets as 'package:version'

    # Multi-package (or single-package fallback): use galaxy-tool-util mulled hash
    try:
        from galaxy.tool_util.deps.mulled.util import build_target, v2_image_name
    except ImportError:
        logger.error(
            "galaxy-tool-util is required for multi-package conda auto-derive "
            "(conda_deps=%s). Install with: pip install galaxy-tool-util",
            conda_deps,
        )
        return None

    targets = []
    for spec in conda_deps:
        pkg, version = _parse_package_spec(spec)
        targets.append(build_target(pkg, version=version or None))

    try:
        image_name = v2_image_name(targets)
        # v2_image_name returns either 'package:version' (single) or
        # 'mulled-v2-<hash>:<hash>' (multi). Both are quay.io/biocontainers refs.
        return f"quay.io/biocontainers/{image_name}"
    except Exception:
        logger.exception("Failed to compute mulled hash for conda_deps=%s", conda_deps)
        return None
