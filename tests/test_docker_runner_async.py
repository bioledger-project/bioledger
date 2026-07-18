"""Tests for DockerRunner's async submit()/poll() lifecycle.

Covers the fix where a single long-blocking `container.wait(timeout=...)`
call was fragile to read-timeouts before the logical deadline. submit()/
poll() decompose this into short, bounded operations so callers can persist
a container_id and reconnect later (e.g. across a CLI process restart).

Requires a working Docker daemon and the `alpine:latest` image. Skips
gracefully if Docker is unavailable.
"""

from __future__ import annotations

import time

import pytest

from bioledger.core.containers.docker import DockerRunner


def test_submit_returns_immediately(docker_available: bool):
    if not docker_available:
        pytest.skip("Docker not available")

    runner = DockerRunner()
    start = time.monotonic()
    container_id = runner.submit(
        image="alpine:latest",
        command=["sleep", "2"],
    )
    elapsed = time.monotonic() - start

    assert isinstance(container_id, str) and container_id
    # submit() must not block for the container's lifetime.
    assert elapsed < 2.0

    # Cleanup: poll until terminal so we don't leak the container.
    result = None
    while result is None:
        time.sleep(0.2)
        result = runner.poll(container_id)
    assert result.exit_code == 0


def test_poll_returns_none_while_running_then_result_when_done(docker_available: bool):
    if not docker_available:
        pytest.skip("Docker not available")

    runner = DockerRunner()
    container_id = runner.submit(image="alpine:latest", command=["sleep", "1.5"])

    # Immediately after submit, the container should still be running.
    assert runner.poll(container_id) is None

    result = None
    deadline = time.monotonic() + 10
    while result is None and time.monotonic() < deadline:
        time.sleep(0.2)
        result = runner.poll(container_id)

    assert result is not None
    assert result.exit_code == 0
    assert result.duration_seconds >= 0


def test_container_not_removed_until_terminal(docker_available: bool):
    """Submitted containers must survive until poll() observes a terminal
    state — required for reconnecting after a process restart."""
    if not docker_available:
        pytest.skip("Docker not available")

    import docker as docker_sdk

    runner = DockerRunner()
    container_id = runner.submit(image="alpine:latest", command=["sleep", "1"])

    # Container should still exist right after submit (not auto-removed).
    client = docker_sdk.from_env()
    container = client.containers.get(container_id)
    assert container is not None

    result = None
    deadline = time.monotonic() + 10
    while result is None and time.monotonic() < deadline:
        time.sleep(0.2)
        result = runner.poll(container_id)
    assert result is not None

    # Once terminal, poll() removes it.
    with pytest.raises(docker_sdk.errors.NotFound):
        client.containers.get(container_id)


def test_poll_handles_notfound_gracefully(docker_available: bool):
    """A container removed externally (or a stale/unknown ID) must not
    raise — poll() should return a synthetic failure RunResult so a poll
    loop doesn't get wedged on a lost job."""
    if not docker_available:
        pytest.skip("Docker not available")

    runner = DockerRunner()
    result = runner.poll("nonexistent-container-id-1234567890")

    assert result is not None
    assert result.exit_code != 0
    assert "not found" in result.stderr.lower()


def test_run_still_works_as_blocking_convenience(docker_available: bool):
    """run() = submit() + local poll loop; behavior must be unchanged."""
    if not docker_available:
        pytest.skip("Docker not available")

    runner = DockerRunner()
    result = runner.run(image="alpine:latest", command=["echo", "hello"])

    assert result.exit_code == 0
    assert "hello" in result.stdout
