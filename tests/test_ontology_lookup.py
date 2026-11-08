"""Tests for the OLS4 ontology lookup (cache + HTTP + reformulation)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

import bioledger.core.ontology.lookup as lookup
from bioledger.core.ontology.lookup import (
    _cache_key,
    _read_cache,
    _write_cache,
    search_ontology,
    search_with_reformulation,
)


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "ontology-cache"
    monkeypatch.setattr(lookup, "_CACHE_DIR", d)
    return d


def _ols_response(docs):
    return SimpleNamespace(
        json=lambda: {"response": {"docs": docs}},
        raise_for_status=lambda: None,
    )


def _doc(label="Term", iri="http://iri/1", ont="obi", desc=None):
    d = {"label": label, "iri": iri, "ontology_name": ont}
    if desc is not None:
        d["description"] = desc
    return d


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
    monkeypatch.setattr(lookup.httpx, "AsyncClient", lambda *a, **k: client)
    return client


class TestCache:
    def test_key_deterministic_and_scoped(self):
        assert _cache_key("q", "obi") == _cache_key("q", "obi")
        assert _cache_key("q", "obi") != _cache_key("q", "efo")
        assert _cache_key("q1", "obi") != _cache_key("q2", "obi")

    def test_write_then_read(self, cache_dir):
        _write_cache("k", [{"a": 1}])
        assert _read_cache("k") == [{"a": 1}]

    def test_missing_key_returns_none(self, cache_dir):
        assert _read_cache("nope") is None

    def test_corrupt_cache_quarantined(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "bad.json").write_text("{not json")
        assert _read_cache("bad") is None
        assert not (cache_dir / "bad.json").exists()  # file removed

    def test_expired_cache_returns_none(self, cache_dir, monkeypatch):
        cache_dir.mkdir(parents=True)
        stale = time.time() - (lookup._CACHE_TTL + 60)
        (cache_dir / "old.json").write_text(
            json.dumps({"ts": stale, "results": [{"x": 1}]})
        )
        assert _read_cache("old") is None


class TestSearchOntology:
    async def test_maps_docs_to_results(self, monkeypatch):
        _patch_client(
            monkeypatch,
            lambda url, params: _ols_response(
                [_doc(label="sequencing", iri="http://x/1", desc=["d1"])]
            ),
        )
        results = await search_ontology("seq", ontology="obi")
        assert results == [
            {
                "label": "sequencing",
                "iri": "http://x/1",
                "ontology": "obi",
                "description": "d1",
            }
        ]

    async def test_request_params(self, monkeypatch):
        client = _patch_client(
            monkeypatch, lambda url, params: _ols_response([_doc()])
        )
        await search_ontology("term", ontology="efo", max_results=5)
        url, params = client.calls[0]
        assert url.endswith("/search")
        assert params["q"] == "term"
        assert params["ontology"] == "efo"
        assert params["rows"] == 5

    async def test_missing_fields_default(self, monkeypatch):
        _patch_client(
            monkeypatch, lambda url, params: _ols_response([{}])
        )
        results = await search_ontology("x", ontology="obi")
        assert results[0]["ontology"] == "obi"  # falls back to query ont
        assert results[0]["label"] == ""
        assert results[0]["description"] == ""

    async def test_results_cached_no_second_call(self, monkeypatch):
        client = _patch_client(
            monkeypatch, lambda url, params: _ols_response([_doc()])
        )
        await search_ontology("q")
        await search_ontology("q")
        assert len(client.calls) == 1

    async def test_empty_results_not_cached(self, monkeypatch):
        client = _patch_client(
            monkeypatch, lambda url, params: _ols_response([])
        )
        await search_ontology("q")
        await search_ontology("q")
        assert len(client.calls) == 2  # empty → no cache write

    async def test_http_error_raises_after_retries(self, monkeypatch):
        def fail(url, params):
            raise httpx.ConnectError("down")

        _patch_client(monkeypatch, fail)
        with pytest.raises(httpx.ConnectError):
            await search_ontology("q")


class TestSearchWithReformulation:
    async def test_first_pass_hit_skips_llm(self, monkeypatch):
        _patch_client(
            monkeypatch, lambda url, params: _ols_response([_doc()])
        )
        results = await search_with_reformulation("q", config=None)
        assert len(results) == 1

    async def test_no_config_returns_empty(self, monkeypatch):
        _patch_client(
            monkeypatch, lambda url, params: _ols_response([])
        )
        assert await search_with_reformulation("q", config=None) == []

    async def test_reformulation_finds_results(self, monkeypatch):
        hits = {"reformulated": [_doc(label="better")]}

        def handler(url, params):
            return _ols_response(hits.get(params["q"], []))

        _patch_client(monkeypatch, handler)

        async def fake_run(prompt):
            return SimpleNamespace(output="'reformulated'")

        monkeypatch.setattr(
            "bioledger.core.llm.agents.make_agent",
            lambda *a, **k: SimpleNamespace(run=fake_run),
        )
        results = await search_with_reformulation("q", config=object())
        assert results[0]["label"] == "better"

    async def test_exhausted_retries_return_empty(self, monkeypatch):
        _patch_client(monkeypatch, lambda url, params: _ols_response([]))

        attempts = []

        async def fake_run(prompt):
            attempts.append(prompt)
            return SimpleNamespace(output=f"try{len(attempts)}")

        monkeypatch.setattr(
            "bioledger.core.llm.agents.make_agent",
            lambda *a, **k: SimpleNamespace(run=fake_run),
        )
        results = await search_with_reformulation(
            "q", config=object(), max_retries=2
        )
        assert results == []
        assert len(attempts) == 2

    async def test_repeated_suggestion_not_retried(self, monkeypatch):
        queries = []

        def handler(url, params):
            queries.append(params["q"])
            return _ols_response([])

        _patch_client(monkeypatch, handler)

        async def fake_run(prompt):
            return SimpleNamespace(output="same")

        monkeypatch.setattr(
            "bioledger.core.llm.agents.make_agent",
            lambda *a, **k: SimpleNamespace(run=fake_run),
        )
        await search_with_reformulation("q", config=object(), max_retries=3)
        # LLM kept suggesting "same" — only searched once
        assert queries == ["q", "same"]
