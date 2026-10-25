"""Native conda environment runner — executes tools via ``conda run``.

Mirrors ``DockerRunner``'s narrow interface (``submit() -> str``,
``poll() -> RunResult | None``) so ``executor.py`` can stay runner-agnostic.
Unlike Docker, conda has no daemon — process state lives in files on disk
in the working directory, so ``poll()`` takes ``workdir`` as an explicit
parameter rather than relying on a daemon-managed handle.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from bioledger.core.engines._subprocess import (
    EXIT_CODE_ENV_VAR,
    EXIT_CODE_FILENAME,
)
from bioledger.core.engines.protocol import RunResult, _is_process_alive


def _conda_binary() -> str:
    """Return the preferred conda-compatible binary on PATH.

    Mamba is CLI-compatible with conda for ``create``/``env list``/``run``
    and is preferred when available for faster env solves.
    """
    mamba = shutil.which("mamba")
    if mamba:
        return mamba
    conda = shutil.which("conda")
    if conda:
        return conda
    raise RuntimeError("conda/mamba not found on PATH")


def _env_name(conda_deps: list[str]) -> str:
    """Deterministic env name from sorted deps."""
    h = hashlib.sha256("|".join(sorted(conda_deps)).encode()).hexdigest()[:12]
    return f"bioledger_{h}"


def _wrap_for_exit_code(command: list[str]) -> list[str]:
    """Wrap a command so the exit code is captured to ``.exit_code``.

    Uses an EXIT trap rather than ``cmd; echo $?`` so the sidecar is
    written even when the command ends with ``exit N`` or aborts under
    ``set -e`` (which would skip a trailing echo). The sidecar path comes
    from ``BIOLEDGER_EXIT_CODE_PATH`` in the environment, so quoting and
    ``cd`` inside the command can't break it.

    ``_to_shell_command()`` upstream returns either a plain argv list or
    ``["sh", "-c", raw_string]``. For the shell-wrapped case, we unwrap and
    re-wrap the raw string directly to avoid double-nesting. For plain argv,
    we shlex-join first.
    """
    if len(command) == 3 and command[0] == "sh" and command[1] == "-c":
        raw = command[2]
    else:
        raw = shlex.join(command)
    wrapped = f"trap 'echo $? > \"${EXIT_CODE_ENV_VAR}\"' EXIT\n{raw}"
    return ["sh", "-c", wrapped]


class CondaRunner:
    """Run tools in native conda environments via ``conda run``.

    Environments are created deterministically (named ``bioledger_<hash>``)
    and cached for reuse across runs. ``submit()`` launches a background
    process with ``start_new_session=True`` so the PID is a process-group
    leader, enabling clean teardown on timeout via ``os.killpg``.
    """

    def ensure_env(self, conda_deps: list[str]) -> str:
        """Create or reuse a conda env for the given deps.

        Returns the env name. Creation is blocking (same precedent as
        Docker's blocking image-pull-on-run).
        """
        env_name = _env_name(conda_deps)
        binary = _conda_binary()

        result = subprocess.run(
            [binary, "env", "list", "--json"],
            capture_output=True, text=True,
        )
        envs = json.loads(result.stdout).get("envs", [])
        if any(Path(p).name == env_name for p in envs):
            return env_name

        subprocess.run(
            [binary, "create", "-n", env_name, *conda_deps, "-y"],
            check=True,
        )
        return env_name

    def submit(
        self,
        conda_env: str,
        command: list[str],
        workdir: Path,
        env: dict | None = None,
    ) -> str:
        """Launch a tool in a conda env and return immediately with the PID.

        stdout/stderr are redirected to ``workdir/stdout.log`` and
        ``workdir/stderr.log``. The exit code is captured to
        ``workdir/.exit_code`` by the wrapping shell.
        """
        binary = _conda_binary()
        argv = [
            binary, "run", "-n", conda_env, "--no-capture-output",
            *_wrap_for_exit_code(command),
        ]

        # Remove a stale sentinel from a previous run in the same dir, and
        # hand the fresh path to the trap via the environment.
        exit_code_path = (workdir / EXIT_CODE_FILENAME).resolve()
        exit_code_path.unlink(missing_ok=True)
        merged_env = dict(os.environ)
        merged_env.update(env or {})
        merged_env[EXIT_CODE_ENV_VAR] = str(exit_code_path)

        stdout_f = open(workdir / "stdout.log", "w")
        stderr_f = open(workdir / "stderr.log", "w")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
                env=merged_env,
            )
        finally:
            stdout_f.close()
            stderr_f.close()

        return str(proc.pid)

    def poll(
        self,
        pid_str: str,
        workdir: Path,
        expected_start_time: float | None = None,
    ) -> RunResult | None:
        """Check if a submitted conda process has terminated.

        Returns ``None`` if still running. Returns a ``RunResult`` once
        the process has terminated — either from the ``.exit_code``
        sentinel file (normal exit) or a synthetic failure (external kill).
        """
        pid = int(pid_str)
        if _is_process_alive(pid, expected_start_time):
            return None

        stdout = ""
        stderr = ""
        stdout_path = workdir / "stdout.log"
        stderr_path = workdir / "stderr.log"
        if stdout_path.exists():
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path.exists():
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")

        exit_code_path = workdir / ".exit_code"
        if exit_code_path.exists():
            try:
                exit_code = int(exit_code_path.read_text().strip())
            except ValueError:
                exit_code = -1
            return RunResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=0.0,
            )

        return RunResult(
            exit_code=-1,
            stdout=stdout,
            stderr=stderr + "\n[process terminated without writing exit code — killed externally?]",
            duration_seconds=0.0,
        )
