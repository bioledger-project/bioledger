from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

import docker
import docker.errors

# Docker's zero-value timestamp for "not yet set" (e.g. FinishedAt on a
# still-running container).
_ZERO_DOCKER_TIME = "0001-01-01T00:00:00Z"

# Statuses that mean "still going" — anything else is treated as terminal.
_NON_TERMINAL_STATUSES = {"created", "running", "restarting", "paused"}


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _parse_docker_timestamp(ts: str) -> datetime:
    """Parse a Docker RFC3339 timestamp (up to nanosecond precision) into a
    timezone-aware datetime. Python's fromisoformat only supports up to
    microsecond precision, so extra fractional digits are truncated."""
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class DockerRunner:
    """Thin wrapper around docker-py for running bioinformatics tools in containers.
    Handles timeouts, memory limits, network isolation, and cleanup.

    Supports both blocking (`run`) and async (`submit` + `poll`) execution.
    Async mode is essential for long-running tools (hours), since a single
    giant HTTP `wait()` call is fragile to read-timeouts well before the
    logical deadline — polling in short bounded increments is far more
    robust and lets callers persist a `container_id` and reconnect later
    (e.g. across a CLI process restart), since the container itself keeps
    running against the Docker daemon independent of our process.
    """

    def __init__(self):
        self.client = docker.from_env()

    def submit(
        self,
        image: str,
        command: list[str] | None = None,
        volumes: dict | None = None,
        env: dict | None = None,
        workdir: str | None = None,
        mem_limit: str = "8g",
        network_disabled: bool = True,
    ) -> str:
        """Start a container and return immediately with its container ID.

        The container is intentionally NOT auto-removed here — removal
        happens in `poll()` once a terminal state is observed, so logs and
        exit code remain retrievable no matter how long the tool runs.
        """
        container = self.client.containers.run(
            image,
            command=command,
            volumes=volumes or {},
            environment=env or {},
            working_dir=workdir,
            detach=True,
            stdout=True,
            stderr=True,
            mem_limit=mem_limit,
            network_mode="none" if network_disabled else "bridge",
        )
        return container.id

    def poll(self, container_id: str) -> RunResult | None:
        """Check on a submitted container without blocking.

        Returns None if the container is still running. Returns a
        RunResult (and removes the container) once it has reached a
        terminal state. If the container was removed externally or the
        daemon lost track of it, returns a synthetic failure RunResult
        rather than raising, so a stale entry doesn't wedge a poll loop.
        """
        try:
            container = self.client.containers.get(container_id)
        except docker.errors.NotFound:
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr=(
                    f"Container {container_id} not found — it may have been "
                    "removed externally or the Docker daemon restarted."
                ),
                duration_seconds=0.0,
            )

        container.reload()
        if container.status in _NON_TERMINAL_STATUSES:
            return None

        # Terminal state: wait() on an already-stopped container returns
        # near-instantly (no real waiting), but bound it defensively.
        wait_result = container.wait(timeout=10)
        stdout = container.logs(stdout=True, stderr=False).decode()
        stderr = container.logs(stdout=False, stderr=True).decode()

        duration_seconds = 0.0
        state = container.attrs.get("State", {})
        started_at, finished_at = state.get("StartedAt", ""), state.get("FinishedAt", "")
        if started_at and finished_at and finished_at != _ZERO_DOCKER_TIME:
            try:
                duration_seconds = (
                    _parse_docker_timestamp(finished_at)
                    - _parse_docker_timestamp(started_at)
                ).total_seconds()
            except Exception:
                duration_seconds = 0.0

        try:
            container.remove(force=True)
        except Exception:
            pass

        return RunResult(
            exit_code=wait_result["StatusCode"],
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
        )

    def run(
        self,
        image: str,
        command: list[str] | None = None,
        volumes: dict | None = None,
        env: dict | None = None,
        workdir: str | None = None,
        timeout: int = 3600,
        mem_limit: str = "8g",
        network_disabled: bool = True,
        poll_interval: float = 0.5,
    ) -> RunResult:
        """Run a command in a container and block until it finishes.

        Implemented as `submit()` followed by a local `poll()` loop rather
        than a single long HTTP wait — see class docstring for why.

        Args:
            image: Docker image URI
            command: Command to run
            volumes: Volume mounts {host_path: {"bind": container_path, "mode": "rw"}}
            env: Environment variables
            workdir: Working directory inside container
            timeout: Max seconds before killing container
            mem_limit: Memory limit (e.g. "8g", "512m")
            network_disabled: If True, container has no network access
            poll_interval: Seconds to sleep between poll attempts
        """
        container_id = self.submit(
            image,
            command=command,
            volumes=volumes,
            env=env,
            workdir=workdir,
            mem_limit=mem_limit,
            network_disabled=network_disabled,
        )
        start = time.monotonic()
        try:
            while True:
                result = self.poll(container_id)
                if result is not None:
                    return result
                if time.monotonic() - start > timeout:
                    raise TimeoutError(
                        f"Container {container_id} exceeded timeout of {timeout}s"
                    )
                time.sleep(poll_interval)
        except Exception:
            try:
                container = self.client.containers.get(container_id)
                container.kill()
            except Exception:
                pass
            try:
                container = self.client.containers.get(container_id)
                container.remove(force=True)
            except Exception:
                pass
            raise
