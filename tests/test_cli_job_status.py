"""Tests for the chat loop's background-job reporting helpers.

_print_job_completion() is called from the auto-poll at the top of each
chat turn; _print_job_status() backs the 'status' command. Both are pure
functions over (console, entry/session), so they're tested directly rather
than driving the full interactive REPL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from bioledger.apps.cli.main import _print_job_completion, _print_job_status
from bioledger.ledger.models import EntryKind, FileRef, LedgerEntry, LedgerSession
from rich.markup import escape as rich_escape


def _render(fn, *args) -> str:
    console = Console(file=None, record=True, width=200)
    fn(console, *args)
    return console.export_text()


def test_print_job_completion_success_shows_outputs(tmp_path: Path):
    output_file = tmp_path / "result.txt"
    output_file.write_text("data")
    entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="fastqc",
        run_status="completed",
        exit_code=0,
        files=[FileRef(path=str(output_file), sha256="abc", size_bytes=4, role="output")],
    )

    text = _render(_print_job_completion, entry)
    assert "finished" in text
    assert "result.txt" in text


def test_print_job_completion_failure_shows_exit_code_and_stderr(tmp_path: Path):
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("USER ERROR: reference file missing\n")
    entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="gatk-haplotypecaller",
        run_status="failed",
        exit_code=1,
        files=[FileRef(path=str(stderr_path), sha256="abc", size_bytes=10, role="log")],
    )

    text = _render(_print_job_completion, entry)
    assert "failed" in text
    assert "exit 1" in text
    assert "USER ERROR: reference file missing" in text


def test_print_job_status_no_running_jobs():
    session = LedgerSession(name="empty")
    text = _render(_print_job_status, session)
    assert "No background jobs running" in text


def test_print_job_status_lists_running_jobs():
    session = LedgerSession(name="with-jobs")
    running_entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="gatk-haplotypecaller",
        run_status="running",
        container_id="abc123",
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=42),
    )
    done_entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="fastqc",
        run_status="completed",
    )
    session.add(running_entry)
    session.add(done_entry)

    text = _render(_print_job_status, session)
    assert "gatk-haplotypecaller" in text
    assert running_entry.id in text
    assert "42s" in text
    # Completed jobs must not show up in the running-jobs listing.
    assert "fastqc" not in text


def test_rich_escape_preserves_brackets_in_tool_names():
    """S8/T6: Tool names containing square brackets must not be swallowed
    by Rich markup.  The review command uses rich_escape() on user-authored
    labels; verify it round-trips bracketed names correctly."""
    raw_name = "my-tool [v2]"
    escaped = rich_escape(raw_name)
    console = Console(file=None, record=True, width=200)
    console.print(f"  (abc123) {escaped}")
    text = console.export_text()
    assert "my-tool [v2]" in text
