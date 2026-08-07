"""Tests for provenance normalization against committed fixture files.

Validates the mapping spec in docs/provenance-normalization.md against
realistic sample files from each engine.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "provenance"


def _parse_duration(duration_str: str) -> float:
    """Parse Nextflow duration strings like '45s', '1m 30s', '2h 15m'."""
    total = 0.0
    parts = duration_str.replace("ms", "ms").split()
    for part in parts:
        if part.endswith("ms"):
            total += float(part[:-2]) / 1000.0
        elif part.endswith("s"):
            total += float(part[:-1])
        elif part.endswith("m"):
            total += float(part[:-1]) * 60
        elif part.endswith("h"):
            total += float(part[:-1]) * 3600
    return total


class TestNextflowTrace:
    def test_fixture_exists(self):
        assert (FIXTURES_DIR / "nextflow_trace.txt").exists()

    def test_parse_trace(self):
        trace_path = FIXTURES_DIR / "nextflow_trace.txt"
        with open(trace_path) as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)

        assert len(rows) == 4
        assert rows[0]["process"] == "samtools_sort"
        assert rows[1]["process"] == "samtools_index"

    def test_duration_parsing(self):
        assert _parse_duration("45s") == 45.0
        assert _parse_duration("1m 30s") == 90.0
        assert _parse_duration("2h 15m") == 8100.0

    def test_exit_status_mapping(self):
        trace_path = FIXTURES_DIR / "nextflow_trace.txt"
        with open(trace_path) as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)

        for row in rows:
            assert row["exit"] == "0"  # all succeeded

    def test_step_names(self):
        trace_path = FIXTURES_DIR / "nextflow_trace.txt"
        with open(trace_path) as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)

        processes = [r["process"] for r in rows]
        assert "samtools_sort" in processes
        assert "samtools_index" in processes


class TestSnakemakeStats:
    def test_fixture_exists(self):
        assert (FIXTURES_DIR / "snakemake_stats.json").exists()

    def test_parse_stats(self):
        stats_path = FIXTURES_DIR / "snakemake_stats.json"
        with open(stats_path) as f:
            stats = json.load(f)

        assert "samtools_sort" in stats
        assert "samtools_index" in stats
        assert "total" in stats

    def test_duration_mapping(self):
        stats_path = FIXTURES_DIR / "snakemake_stats.json"
        with open(stats_path) as f:
            stats = json.load(f)

        sort_stats = stats["samtools_sort"]
        assert sort_stats["mean-seconds"] == 43.5
        assert sort_stats["total-seconds"] == 87.0

    def test_job_count(self):
        stats_path = FIXTURES_DIR / "snakemake_stats.json"
        with open(stats_path) as f:
            stats = json.load(f)

        assert stats["samtools_sort"]["jobs"] == 2
        assert stats["samtools_index"]["jobs"] == 2
        assert stats["total"]["jobs"] == 4

    def test_exit_codes(self):
        stats_path = FIXTURES_DIR / "snakemake_stats.json"
        with open(stats_path) as f:
            stats = json.load(f)

        assert stats["samtools_sort"]["exit-code"] == {"0": 2}
        assert stats["total"]["exit-code"] == {"0": 4}


class TestGalaxyInvocation:
    def test_fixture_exists(self):
        assert (FIXTURES_DIR / "galaxy_invocation.json").exists()

    def test_parse_invocation(self):
        inv_path = FIXTURES_DIR / "galaxy_invocation.json"
        with open(inv_path) as f:
            inv = json.load(f)

        assert inv["state"] == "scheduled"
        assert len(inv["steps"]) == 2

    def test_step_names(self):
        inv_path = FIXTURES_DIR / "galaxy_invocation.json"
        with open(inv_path) as f:
            inv = json.load(f)

        tool_ids = [s["tool_id"] for s in inv["steps"]]
        assert any("samtools_sort" in tid for tid in tool_ids)
        assert any("samtools_index" in tid for tid in tool_ids)

    def test_tool_identity(self):
        inv_path = FIXTURES_DIR / "galaxy_invocation.json"
        with open(inv_path) as f:
            inv = json.load(f)

        for step in inv["steps"]:
            assert step["tool_version"] == "1.17"

    def test_exit_status_mapping(self):
        inv_path = FIXTURES_DIR / "galaxy_invocation.json"
        with open(inv_path) as f:
            inv = json.load(f)

        for step in inv["steps"]:
            assert step["job_state"] == "ok"  # maps to exit_status=0

    def test_duration_from_timestamps(self):
        inv_path = FIXTURES_DIR / "galaxy_invocation.json"
        with open(inv_path) as f:
            inv = json.load(f)

        # Step 0: 10:00:05 → 10:00:50 = 45 seconds
        step0 = inv["steps"][0]
        from datetime import datetime
        create = datetime.fromisoformat(step0["create_time"].replace("Z", "+00:00"))
        update = datetime.fromisoformat(step0["update_time"].replace("Z", "+00:00"))
        duration = (update - create).total_seconds()
        assert duration == 45.0
