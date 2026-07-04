#!/usr/bin/env python3
"""Build the BioLedger GitHub Pages site.

Usage:
    python scripts/build_pages.py --out _site

Copies the hand-authored landing page (``pages/index.html``, ``pages/style.css``,
``docs/assets/logo.png``) verbatim, and auto-generates an Examples section from
``examples/*/README.md`` so it stays in sync with the repo without hand
duplication. Rewrites relative links that point outside the example's own
directory (e.g. ``../../README.md``) to absolute GitHub blob URLs, since the
published site doesn't carry the rest of the repo with it.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import markdown

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_BLOB_BASE = "https://github.com/d-callan/bioledger/blob/main"
GITHUB_TREE_BASE = "https://github.com/d-callan/bioledger/tree/main"

MD_EXTENSIONS = ["tables", "fenced_code", "toc"]

NAV = """
<nav class="top">
  <a href="../index.html#top">Home</a>
  <a href="../index.html#documentation">Documentation</a>
  <a href="index.html">Examples</a>
  <a href="https://bioledger-project.github.io/toolspec-library/">Tool Library</a>
  <a href="https://bioledger-project.github.io/isatab-library/">Study Library</a>
  <a href="https://github.com/d-callan/bioledger">GitHub</a>
</nav>
""".strip()

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="../style.css">
</head>
<body>
  <div class="page">
    {nav}
    {body}
  </div>
</body>
</html>
"""

_LINK_RE = re.compile(r"(\]\()(\.\./\.\./[^)#]+)(#[^)]*)?(\))")


def _rewrite_relative_links(markdown_text: str, example_dir: Path) -> str:
    """Rewrite links that escape the example directory to absolute GitHub URLs."""

    def replace(match: re.Match) -> str:
        prefix, rel_path, anchor, suffix = match.groups()
        target = (example_dir / rel_path).resolve()
        try:
            repo_rel = target.relative_to(REPO_ROOT)
        except ValueError:
            return match.group(0)
        url = f"{GITHUB_BLOB_BASE}/{repo_rel.as_posix()}{anchor or ''}"
        return f"{prefix}{url}{suffix}"

    return _LINK_RE.sub(replace, markdown_text)


def _markdown_to_plain(text: str) -> str:
    """Render inline markdown (bold, links, etc.) and strip tags for card blurbs."""
    html = markdown.markdown(text)
    return re.sub(r"<[^>]+>", "", html).strip()


def _extract_title_and_blurb(markdown_text: str) -> tuple[str, str]:
    lines = markdown_text.splitlines()
    title = ""
    blurb = ""
    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if title and not blurb and line.strip() and not line.startswith("#"):
            blurb = _markdown_to_plain(line.strip().lstrip(">").strip())
            break
    return title, blurb


def render_markdown(text: str) -> str:
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


def wrap_page(title: str, body_html: str) -> str:
    return PAGE_TEMPLATE.format(title=title, nav=NAV, body=body_html)


def discover_examples() -> list[Path]:
    examples_dir = REPO_ROOT / "examples"
    return sorted(p for p in examples_dir.iterdir() if p.is_dir() and (p / "README.md").exists())


def build_examples(out_dir: Path) -> list[dict]:
    examples_out = out_dir / "examples"
    examples_out.mkdir(parents=True, exist_ok=True)

    cards = []
    for example_dir in discover_examples():
        name = example_dir.name
        readme_text = (example_dir / "README.md").read_text()
        title, blurb = _extract_title_and_blurb(readme_text)
        title = title or name
        rewritten = _rewrite_relative_links(readme_text, example_dir)

        page_dir = examples_out / name
        page_dir.mkdir(parents=True, exist_ok=True)
        html = wrap_page(f"BioLedger — {title}", render_markdown(rewritten))
        (page_dir / "index.html").write_text(html)

        cards.append(
            {
                "name": name,
                "title": title,
                "blurb": blurb,
                "href": f"{name}/index.html",
                "source": f"{GITHUB_TREE_BASE}/examples/{name}",
            }
        )

    card_html = "\n".join(
        f"""
        <div class="card">
          <h3><a href="{c['href']}">{c['title']}</a></h3>
          <p>{c['blurb']}</p>
          <p><a href="{c['source']}">View source on GitHub &rarr;</a></p>
        </div>
        """.strip()
        for c in cards
    )
    index_body = f"""
    <header class="hero">
      <h1>Examples</h1>
      <p class="tagline">End-to-end walkthroughs, generated from the
      <code>examples/</code> directory.</p>
    </header>
    <div class="card-grid">
      {card_html}
    </div>
    """
    (examples_out / "index.html").write_text(wrap_page("BioLedger — Examples", index_body))
    return cards


def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(REPO_ROOT / "pages" / "index.html", out_dir / "index.html")
    shutil.copy(REPO_ROOT / "pages" / "style.css", out_dir / "style.css")

    assets_out = out_dir / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "docs" / "assets" / "logo.png", assets_out / "logo.png")

    build_examples(out_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="_site", help="Output directory")
    args = parser.parse_args()
    build(Path(args.out))
    print(f"Wrote site to {args.out}/")


if __name__ == "__main__":
    main()
