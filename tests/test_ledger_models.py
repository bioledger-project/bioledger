from __future__ import annotations

from bioledger.ledger.models import (
    ChatMessage,
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
    SessionStatus,
)


def test_ledger_entry_defaults():
    entry = LedgerEntry(kind=EntryKind.TOOL_RUN)
    assert entry.kind == EntryKind.TOOL_RUN
    assert entry.id  # auto-generated UUID
    assert entry.timestamp
    assert entry.files == []
    assert entry.params == {}
    assert entry.exit_code is None
    assert entry.parent_id is None


def test_ledger_entry_with_files():
    entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="fastqc",
        files=[
            FileRef(path="/data/reads.fastq", sha256="abc123", size_bytes=1000, role="input"),
            FileRef(path="/output/report.html", sha256="def456", size_bytes=500, role="output"),
        ],
        container=ContainerInfo(
            image="quay.io/biocontainers/fastqc:0.11.9--0",
            command=["fastqc", "reads.fastq"],
        ),
        exit_code=0,
        duration_seconds=12.5,
    )
    assert len(entry.files) == 2
    assert entry.container.image == "quay.io/biocontainers/fastqc:0.11.9--0"
    assert entry.exit_code == 0


def test_ledger_session_defaults():
    session = LedgerSession()
    assert session.id
    assert session.status == SessionStatus.ACTIVE
    assert session.entries == []
    assert session.chat_messages == []
    assert session.name == ""


def test_ledger_session_add_entry():
    session = LedgerSession(name="Test")
    entry = LedgerEntry(kind=EntryKind.DATA_IMPORT)
    session.add(entry)
    assert len(session.entries) == 1
    assert session.entries[0].id == entry.id


def test_ledger_session_add_message():
    session = LedgerSession()
    session.add_message("user", "Hello", forge="test")
    assert len(session.chat_messages) == 1
    assert session.chat_messages[0].role == "user"
    assert session.chat_messages[0].content == "Hello"
    assert session.chat_messages[0].forge == "test"


def test_chat_message_creation():
    msg = ChatMessage(role="assistant", content="Hi there", forge="analysisforge")
    assert msg.role == "assistant"
    assert msg.timestamp


def test_entry_kind_enum():
    assert EntryKind.TOOL_RUN.value == "tool_run"
    assert EntryKind.SCRIPT_RUN.value == "script_run"
    assert EntryKind.DATA_IMPORT.value == "data_import"
    assert EntryKind.LLM_CALL.value == "llm_call"


def test_session_status_enum():
    assert SessionStatus.ACTIVE.value == "active"
    assert SessionStatus.ARCHIVED.value == "archived"


def test_container_info():
    ci = ContainerInfo(
        image="ubuntu:latest",
        command=["echo", "hello"],
        volumes={"/data": "/mnt/data"},
    )
    assert ci.image == "ubuntu:latest"
    assert ci.command == ["echo", "hello"]


def test_file_ref():
    fr = FileRef(path="/tmp/file.bam", sha256="abc", size_bytes=1024, role="output")
    assert fr.role == "output"
    assert fr.size_bytes == 1024


def test_ledger_entry_serialization():
    entry = LedgerEntry(
        kind=EntryKind.TOOL_RUN,
        tool_spec_name="bwa",
        exit_code=0,
    )
    json_str = entry.model_dump_json()
    loaded = LedgerEntry.model_validate_json(json_str)
    assert loaded.kind == EntryKind.TOOL_RUN
    assert loaded.tool_spec_name == "bwa"


def test_ledger_session_serialization():
    session = LedgerSession(name="Test Session", description="A test")
    session.add(LedgerEntry(kind=EntryKind.DATA_IMPORT))
    session.add_message("user", "Hello")
    json_str = session.model_dump_json()
    loaded = LedgerSession.model_validate_json(json_str)
    assert loaded.name == "Test Session"
    assert len(loaded.entries) == 1
    assert len(loaded.chat_messages) == 1


class TestAsyncRunStatus:
    """Tests for run_status/container_id (async tool execution tracking)."""

    def test_default_run_status_is_completed(self):
        """Default preserves the meaning of pre-existing entries with no
        migration needed — a bare LedgerEntry() is assumed already done."""
        entry = LedgerEntry(kind=EntryKind.TOOL_RUN)
        assert entry.run_status == "completed"
        assert entry.container_id is None

    def test_running_entry_with_container_id(self):
        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            run_status="running",
            container_id="abc123def456",
        )
        assert entry.run_status == "running"
        assert entry.container_id == "abc123def456"
        assert entry.exit_code is None

    def test_run_status_round_trip_serialization(self):
        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            run_status="running",
            container_id="deadbeef",
        )
        loaded = LedgerEntry.model_validate_json(entry.model_dump_json())
        assert loaded.run_status == "running"
        assert loaded.container_id == "deadbeef"

    def test_backward_compat_load_without_new_fields(self):
        """Entries persisted before run_status/container_id existed must
        still load, defaulting run_status to 'completed'."""
        old_style_json = (
            '{"kind": "tool_run", "tool_spec_name": "bwa", "exit_code": 0}'
        )
        loaded = LedgerEntry.model_validate_json(old_style_json)
        assert loaded.run_status == "completed"
        assert loaded.container_id is None
        assert loaded.exit_code == 0

    def test_all_run_status_values_accepted(self):
        for status in ("pending", "running", "completed", "failed", "unknown"):
            entry = LedgerEntry(kind=EntryKind.TOOL_RUN, run_status=status)
            assert entry.run_status == status
