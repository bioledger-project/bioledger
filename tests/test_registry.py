"""Tests for the BioContainers registry client (cache + image resolution)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

import bioledger.forges.toolforge.registry as registry
from bioledger.forges.toolforge.registry import (
    _read_cache,
    _write_cache,
    get_container_image,
    search_biocontainers,
)


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "biocontainers-cache"
    monkeypatch.setattr(registry, "_CACHE_DIR", d)
    return d


_TOOLS = [
    {
        "name": "samtools",
        "versions": [
            {
                "name": "1.20",
                "images": [
                    {
                        "image_type": "Docker",
                        "image_name": "quay.io/biocontainers/samtools:1.20--h50ea8bc_1",
                    },
                    {"image_type": "Singularity", "image_name": "sing.img"},
                ],
            },
            {
                "name": "1.19",
                "images": [
                    {
                        "image_type": "Docker",
                        "image_name": "quay.io/biocontainers/samtools:1.19--x",
                    }
                ],
            },
        ],
    }
]


def _response(payload):
    return SimpleNamespace(
        json=lambda: payload, raise_for_status=lambda: None
    )


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return self._handler(url, params)


def _patch_client(monkeypatch, handler):
    client = _FakeClient(handler)
    monkeypatch.setattr(registry.httpx, "AsyncClient", lambda *a, **k: client)
    return client


class TestCache:
    def test_write_then_read(self, cache_dir):
        _write_cache("tool", [{"x": 1}])
        assert _read_cache("tool") == [{"x": 1}]

    def test_miss_returns_none(self, cache_dir):
        assert _read_cache("ghost") is None

    def test_corrupt_cache_is_miss(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "bad.json").write_text("{nope")
        assert _read_cache("bad") is None

    def test_expired_cache_is_miss(self, cache_dir):
        cache_dir.mkdir(parents=True)
        stale = time.time() - (registry._CACHE_TTL + 60)
        (cache_dir / "old.json").write_text(
            json.dumps({"ts": stale, "results": [{"x": 1}]})
        )
        assert _read_cache("old") is None


class TestSearchBiocontainers:
    async def test_hits_api_and_caches(self, cache_dir, monkeypatch):
        client = _patch_client(
            monkeypatch, lambda url, params: _response(_TOOLS)
        )
        results = await search_biocontainers("samtools")
        assert results[0]["name"] == "samtools"
        url, params = client.calls[0]
        assert params["name"] == "samtools"
        # second call served from cache
        await search_biocontainers("samtools")
        assert len(client.calls) == 1

    async def test_empty_results_not_cached(self, monkeypatch):
        client = _patch_client(monkeypatch, lambda url, params: _response([]))
        await search_biocontainers("x")
        await search_biocontainers("x")
        assert len(client.calls) == 2

    async def test_retries_then_raises(self, monkeypatch):
        calls = []

        def fail(url, params):
            calls.append(1)
            raise httpx.ConnectError("down")

        _patch_client(monkeypatch, fail)
        with pytest.raises(httpx.ConnectError):
            await search_biocontainers("x")
        assert len(calls) == 3  # tenacity stop_after_attempt(3)


class TestGetContainerImage:
    async def test_first_version_by_default(self, monkeypatch):
        _patch_client(monkeypatch, lambda url, params: _response(_TOOLS))
        img = await get_container_image("samtools")
        assert img == "quay.io/biocontainers/samtools:1.20--h50ea8bc_1"

    async def test_version_substring_match(self, monkeypatch):
        _patch_client(monkeypatch, lambda url, params: _response(_TOOLS))
        img = await get_container_image("samtools", version="1.19")
        assert img == "quay.io/biocontainers/samtools:1.19--x"

    async def test_unknown_version_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, lambda url, params: _response(_TOOLS))
        assert await get_container_image("samtools", version="9.9") is None

    async def test_no_docker_image_returns_none(self, monkeypatch):
        tools = [
            {
                "versions": [
                    {
                        "name": "1",
                        "images": [
                            {"image_type": "Singularity", "image_name": "s.img"}
                        ],
                    }
                ]
            }
        ]
        _patch_client(monkeypatch, lambda url, params: _response(tools))
        assert await get_container_image("tool") is None

    async def test_no_versions_returns_none(self, monkeypatch):
        _patch_client(
            monkeypatch,
            lambda url, params: _response([{"versions": []}]),
        )
        assert await get_container_image("tool") is None

    async def test_empty_results_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, lambda url, params: _response([]))
        assert await get_container_image("ghost") is None

    async def test_api_failure_falls_back_to_stale_cache(
        self, cache_dir, monkeypatch
    ):
        # Pre-seed an expired cache entry
        cache_dir.mkdir(parents=True)
        stale = time.time() - (registry._CACHE_TTL + 60)
        (cache_dir / "samtools.json").write_text(
            json.dumps({"ts": stale, "results": _TOOLS})
        )

        def fail(url, params):
            raise httpx.ConnectError("down")

        _patch_client(monkeypatch, fail)
        # API unreachable but stale cache exists → still resolves
        img = await get_container_image("samtools")
        assert img == "quay.io/biocontainers/samtools:1.20--h50ea8bc_1"

    async def test_api_failure_no_cache_returns_none(self, monkeypatch):
        def fail(url, params):
            raise httpx.ConnectError("down")

        _patch_client(monkeypatch, fail)
        assert await get_container_image("tool") is None
