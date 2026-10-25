"""Shared engine version detection helpers.

Used by the engine adapters (to capture the version at submit time) and by
``crystallize.generate_engine_env_yml`` (to pin versions in the exported
environment file). Centralizing the version parsing avoids duplication.
"""

from __future__ import annotations

import re
import shutil
import subprocess


# Per-engine version command mapping.
# nextflow uses `-version` (not `--version`); galaxy uses `planemo --version`.
_VERSION_COMMANDS: dict[str, list[str]] = {
    "snakemake": ["snakemake", "--version"],
    "nextflow": ["nextflow", "-version"],
    "galaxy": ["planemo", "--version"],
}


def _extract_version(engine: str, raw: str) -> str:
    """Extract a version string from engine ``--version`` output.

    Handles different output formats:
    - snakemake: "7.32.4" (just the version)
    - nextflow: "Nextflow version 23.10.0 build 5891" (version is 3rd word)
    - galaxy/planemo: "planemo 0.74.0" (version is 2nd word)
    Falls back to the first word that looks like a semver (x.y.z).
    """
    parts = raw.split()
    semver_match = re.search(r"\d+\.\d+\.\d+", raw)
    if semver_match:
        return semver_match.group(0)
    return parts[-1] if parts else "latest"


def detect_engine_version(engine_name: str, executable: str | None = None) -> str:
    """Detect the version of an engine by shelling out once.

    Args:
        engine_name: One of "snakemake", "nextflow", "galaxy".
        executable: Optional override for the binary path. If None, uses
            the default binary name from ``_VERSION_COMMANDS``.

    Returns:
        A version string like "snakemake 7.32.4" (engine name + extracted
        semver), or "<engine_name> unknown" if detection fails.
    """
    cmd = _VERSION_COMMANDS.get(engine_name)
    if cmd is None:
        return f"{engine_name} unknown"

    binary = executable or cmd[0]
    full_cmd = [binary, *cmd[1:]]

    if shutil.which(binary) is None:
        return f"{engine_name} unknown"

    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or result.stderr).strip()
        # Scan the full output, not just the first line — `nextflow
        # -version` prints a multi-line banner ("N E X T F L O W" logo)
        # whose first line contains no version.
        ver = _extract_version(engine_name, output)
        return f"{engine_name} {ver}"
    except Exception:
        return f"{engine_name} unknown"
