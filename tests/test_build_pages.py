"""Smoke tests for the GitHub Pages site builder (scripts/build_pages.py).

``scripts/`` isn't part of the installed package, so it's imported directly
via sys.path rather than as ``bioledger.*``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_pages  # noqa: E402


def test_discover_examples_finds_known_examples():
    names = {p.name for p in build_pages.discover_examples()}
    assert {"hello_bioledger", "galaxy_tool_import", "csv_to_isatab", "variant_calling"} <= names


def test_build_creates_expected_files(tmp_path):
    out_dir = tmp_path / "_site"
    build_pages.build(out_dir)

    assert (out_dir / "index.html").exists()
    assert (out_dir / "style.css").exists()
    assert (out_dir / "assets" / "logo.png").exists()
    assert (out_dir / "examples" / "index.html").exists()

    for example_dir in build_pages.discover_examples():
        assert (out_dir / "examples" / example_dir.name / "index.html").exists()


def test_rewrite_relative_links_points_to_github():
    example_dir = REPO_ROOT / "examples" / "hello_bioledger"
    text = "See the main [README](../../README.md#prerequisites) for setup."
    rewritten = build_pages._rewrite_relative_links(text, example_dir)
    assert "https://github.com/d-callan/bioledger/blob/main/README.md#prerequisites" in rewritten


def test_extract_title_and_blurb():
    text = "# My Title\n\nSome intro paragraph.\n\nMore text."
    title, blurb = build_pages._extract_title_and_blurb(text)
    assert title == "My Title"
    assert blurb == "Some intro paragraph."
