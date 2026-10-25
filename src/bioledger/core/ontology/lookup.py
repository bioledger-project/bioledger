from __future__ import annotations

import hashlib
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

OLS_API = "https://www.ebi.ac.uk/ols4/api"
_CACHE_DIR = Path.home() / ".bioledger" / "cache" / "ontology"
_CACHE_TTL = 86400 * 7  # 7 days — ontology terms change infrequently


def _cache_key(query: str, ontology: str) -> str:
    """Deterministic cache key for a query+ontology pair."""
    return hashlib.md5(f"{ontology}:{query}".encode()).hexdigest()


def _read_cache(key: str) -> list[dict] | None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupt cache file — quarantine it rather than failing
            path.unlink(missing_ok=True)
            return None
        if time.time() - data.get("ts", 0) < _CACHE_TTL:
            return data["results"]
    return None


def _write_cache(key: str, results: list[dict]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"ts": time.time(), "results": results})
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
    ),
)
async def search_ontology(
    query: str,
    ontology: str = "obi",
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Search OLS4 for ontology terms matching a query.

    Returns list of dicts with keys: label, iri, ontology, description.
    Results are cached to disk for 7 days.
    """
    key = _cache_key(query, ontology)
    cached = _read_cache(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{OLS_API}/search",
            params={
                "q": query,
                "ontology": ontology,
                "rows": max_results,
                "exact": "false",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for doc in data.get("response", {}).get("docs", []):
        results.append(
            {
                "label": doc.get("label", ""),
                "iri": doc.get("iri", ""),
                "ontology": doc.get("ontology_name", ontology),
                "description": (doc.get("description") or [""])[0],
            }
        )

    # Don't cache empty results — a transient OLS outage or unlucky
    # query shouldn't poison the cache for a week.
    if results:
        _write_cache(key, results)
    return results


async def search_with_reformulation(
    query: str,
    ontology: str = "obi",
    config: Any = None,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """Search ontology with LLM-driven query reformulation on empty results.

    If the initial search returns nothing, asks the LLM to reformulate the query
    (e.g. expand abbreviations, try synonyms) and retries up to max_retries times.
    """
    results = await search_ontology(query, ontology=ontology)
    if results:
        return results

    if config is None:
        return []  # no LLM available for reformulation

    from bioledger.core.llm.agents import make_agent

    reformulate_agent = make_agent(
        config,
        task="ontology_reformulate",
        instructions=(
            "You are an ontology search expert. The user's search query returned no results. "
            "Reformulate the query to find the intended ontology term. "
            "Try synonyms, expanded abbreviations, or alternative phrasings. "
            "Return ONLY the reformulated query string, nothing else."
        ),
        output_type=str,
    )

    original_query = query
    tried: list[str] = [query]
    for attempt in range(max_retries):
        prompt = (
            f"Original query: '{original_query}'\n"
            f"Ontology: {ontology}\n"
            f"Attempt {attempt + 1}/{max_retries}\n"
            f"Previous queries that returned nothing: "
            f"{', '.join(repr(q) for q in tried)}\n"
            f"Suggest a better search query."
        )
        result = await reformulate_agent.run(prompt)
        new_query = result.output.strip().strip("'\"")

        if new_query and new_query.lower() not in {q.lower() for q in tried}:
            results = await search_ontology(new_query, ontology=ontology)
            if results:
                return results
            tried.append(new_query)

    return []  # all reformulations exhausted
