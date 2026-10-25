from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

BIOCONTAINERS_API = "https://api.biocontainers.pro/ga4gh/trs/v2"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "biocontainers"
_CACHE_TTL = 86400  # 24 hours


def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> list[dict] | None:
    path = _cache_path(key)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None  # corrupt cache — treat as a miss
        if time.time() - data.get("ts", 0) < _CACHE_TTL:
            return data["results"]
    return None


def _write_cache(key: str, results: list[dict]) -> None:
    _cache_path(key).write_text(json.dumps({"ts": time.time(), "results": results}))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
    ),
)
async def search_biocontainers(tool_name: str) -> list[dict[str, Any]]:
    """Search BioContainers registry with retry + cache."""
    cached = _read_cache(tool_name)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{BIOCONTAINERS_API}/tools",
            params={"name": tool_name, "limit": 10},
        )
        resp.raise_for_status()
        results = resp.json()
    if results:
        # Don't 24h-cache an empty result — a tool may be published
        # (or the API may have blipped) within the TTL.
        _write_cache(tool_name, results)
    return results


async def get_container_image(tool_name: str, version: str | None = None) -> str | None:
    """Resolve a tool name to a Docker image URI from BioContainers.
    Returns None (with warning) if the API is unreachable after retries."""
    try:
        results = await search_biocontainers(tool_name)
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException):
        # All retries exhausted — fall back to stale cache
        cached = _read_cache(tool_name)
        if cached:
            results = cached  # stale but better than nothing
        else:
            return None
    if not results:
        return None
    tool = results[0]
    versions = tool.get("versions", [])
    if version:
        v = next((v for v in versions if version in v.get("name", "")), None)
    else:
        v = versions[0] if versions else None
    if not v:
        return None
    images = v.get("images", [])
    docker_img = next((img for img in images if img.get("image_type") == "Docker"), None)
    return docker_img.get("image_name") if docker_img else None
