"""Remote library client for BioLedger tool and study indexes.

Fetches static JSON indexes from GitHub Pages (or any URL), caches them
locally, and provides search/list operations.  The core entry points:

    ToolLibrary  — wraps the toolspec-library index
    StudyLibrary — wraps the isatab-library index

Both use LibraryClient internally for fetch+cache logic.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from bioledger_isatab_schema.utils import warn_on_ambiguous_periods

logger = logging.getLogger(__name__)

# Default GitHub Pages URLs (public repos under bioledger-project org)
DEFAULT_TOOLSPEC_INDEX_URL = (
    "https://bioledger-project.github.io/toolspec-library/index.json"
)
DEFAULT_STUDY_INDEX_URL = (
    "https://bioledger-project.github.io/isatab-library/index.json"
)

# Cache freshness: re-fetch if older than this (seconds)
CACHE_TTL_SECONDS = 3600  # 1 hour


class LibraryClient:
    """Generic JSON index fetcher with local file cache."""

    def __init__(self, url: str, cache_path: Path, ttl: int = CACHE_TTL_SECONDS):
        self.url = url
        self.cache_path = cache_path
        self.ttl = ttl
        self._entries: list[dict[str, Any]] | None = None

    def _is_cache_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age = time.time() - self.cache_path.stat().st_mtime
        return age < self.ttl

    def refresh(self) -> list[dict[str, Any]]:
        """Force-fetch the index from the remote URL and update cache."""
        try:
            resp = httpx.get(self.url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            entries = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch library index from %s: %s", self.url, e)
            # Fall back to stale cache if available
            if self.cache_path.exists():
                entries = json.loads(self.cache_path.read_text())
            else:
                entries = []
            self._entries = entries
            return entries

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(entries, indent=2))
        self._entries = entries
        return entries

    def entries(self) -> list[dict[str, Any]]:
        """Return cached entries, fetching if stale or missing."""
        if self._entries is not None:
            return self._entries
        if self._is_cache_fresh():
            self._entries = json.loads(self.cache_path.read_text())
            return self._entries
        return self.refresh()


class ToolLibrary:
    """High-level interface for the remote toolspec library."""

    def __init__(self, cache_dir: Path, url: str = DEFAULT_TOOLSPEC_INDEX_URL):
        self._client = LibraryClient(
            url=url, cache_path=cache_dir / "toolspec_index.json"
        )

    def refresh(self) -> None:
        self._client.refresh()

    def list_all(self) -> list[dict[str, Any]]:
        """Return all tool index entries."""
        return self._client.entries()

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search tools by substring match across name, family, description, categories."""
        q = query.lower()
        results = []
        for entry in self._client.entries():
            if (
                q in entry.get("name", "").lower()
                or q in entry.get("family", "").lower()
                or q in entry.get("description", "").lower()
                or any(q in c.lower() for c in entry.get("categories", []))
                or any(q in i.lower() for i in entry.get("inputs", []))
                or any(q in o.lower() for o in entry.get("outputs", []))
            ):
                results.append(entry)
        return results

    def get(self, name: str) -> dict[str, Any] | None:
        """Get a specific tool entry by exact name."""
        for entry in self._client.entries():
            if entry.get("name") == name:
                return entry
        return None

    def list_cached(self) -> list[dict[str, Any]]:
        """Return cached entries without triggering a network fetch.

        Returns empty list if no cache exists (deferred fetch).
        """
        if self._client.cache_path.exists():
            try:
                return json.loads(self._client.cache_path.read_text())
            except Exception:
                return []
        return []


class StudyLibrary:
    """High-level interface for the remote ISA-Tab study library."""

    def __init__(self, cache_dir: Path, url: str = DEFAULT_STUDY_INDEX_URL):
        self._client = LibraryClient(
            url=url, cache_path=cache_dir / "study_index.json"
        )

    def refresh(self) -> None:
        self._client.refresh()

    def list_all(self) -> list[dict[str, Any]]:
        """Return all study index entries."""
        return self._client.entries()

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search studies by substring match across accession, organism, title, etc."""
        q = query.lower()
        results = []
        for entry in self._client.entries():
            if (
                q in entry.get("accession", "").lower()
                or q in entry.get("organism", "").lower()
                or q in entry.get("title", "").lower()
                or q in entry.get("description", "").lower()
                or q in entry.get("study_type", "").lower()
                or any(q in f.lower() for f in entry.get("formats", []))
            ):
                results.append(entry)
        return results

    def get(self, accession: str) -> dict[str, Any] | None:
        """Get a specific study entry by accession."""
        for entry in self._client.entries():
            if entry.get("accession") == accession:
                return entry
        return None

    async def download(
        self,
        accession: str,
        dest_dir: Path,
        with_data: bool = True,
        raw_base_url: str = "https://raw.githubusercontent.com/bioledger-project/isatab-library/main/studies",
    ) -> Path:
        """Download a study from the remote library to a local directory.

        Fetches the manifest.yaml, ISA structural files (from isa_files in the
        index), and optionally the data files declared in the manifest.

        Args:
            accession: Study accession to download.
            dest_dir: Local directory to save files (created if missing).
            with_data: If True, download remote data files via manifest.
            raw_base_url: Base URL for raw content from the library repo.

        Returns:
            Path to the downloaded study directory.

        Raises:
            ValueError: If the study is not found in the index or download fails.
        """
        entry = self.get(accession)
        if not entry:
            raise ValueError(f"Study '{accession}' not found in library index")

        study_path = entry.get("path", accession)
        base_url = f"{raw_base_url}/{study_path}"
        isa_files = entry.get("isa_files", [])

        dest_dir.mkdir(parents=True, exist_ok=True)

        # Fetch manifest.yaml
        manifest_url = f"{base_url}/manifest.yaml"
        try:
            resp = httpx.get(manifest_url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            manifest_path = dest_dir / "manifest.yaml"
            manifest_path.write_text(resp.text)
            logger.info("Downloaded manifest.yaml for %s", accession)
        except Exception as e:
            raise ValueError(f"Failed to fetch manifest for {accession}: {e}") from e

        # Fetch ISA structural files
        for fname in isa_files:
            url = f"{base_url}/{fname}"
            try:
                resp = httpx.get(url, timeout=15, follow_redirects=True)
                if resp.status_code == 200:
                    (dest_dir / fname).write_text(resp.text)
                    logger.info("Downloaded %s for %s", fname, accession)
            except Exception:
                logger.warning("Failed to fetch %s for %s", fname, accession)

        if with_data:
            # Load manifest and download data files
            from bioledger_isatab_schema.manifest import load_manifest
            from bioledger_isatab_schema.download import download_manifest

            manifest = load_manifest(dest_dir)
            if manifest is None:
                raise ValueError(f"Failed to parse manifest.yaml in {dest_dir}")

            await download_manifest(manifest, dest_dir, user_confirmed=True)
            logger.info("Downloaded data files for %s", accession)

            # Warn on filenames with ambiguous periods (e.g. NCBI accessions)
            for fpath in dest_dir.iterdir():
                if fpath.is_file() and fpath.name not in ("manifest.yaml",):
                    warn_on_ambiguous_periods(fpath.name)

        return dest_dir
