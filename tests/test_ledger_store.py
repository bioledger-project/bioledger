from __future__ import annotations

import pytest

from bioledger.ledger.models import (
    ChatMessage,
    EntryKind,
    LedgerEntry,
    LedgerSession,
    SessionStatus,
)
from bioledger.ledger.store import LedgerStore


def test_create_and_load_session(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="Test", description="A test session")
    store.create_session(session)

    loaded = store.load_session(session.id)
    assert loaded.id == session.id
    assert loaded.name == "Test"
    assert loaded.description == "A test session"
    assert loaded.status == SessionStatus.ACTIVE


def test_save_session_with_entries(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="With entries")
    store.create_session(session)

    entry = LedgerEntry(kind=EntryKind.TOOL_RUN, tool_spec_name="fastqc", exit_code=0)
    session.add(entry)
    store.save_session(session)

    loaded = store.load_session(session.id)
    assert len(loaded.entries) == 1
    assert loaded.entries[0].tool_spec_name == "fastqc"


def test_list_sessions(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    s1 = LedgerSession(name="First")
    s2 = LedgerSession(name="Second")
    store.create_session(s1)
    store.create_session(s2)

    rows = store.list_sessions()
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert "First" in names
    assert "Second" in names


def test_list_sessions_filter_by_status(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    s1 = LedgerSession(name="Active")
    store.create_session(s1)
    store.archive_session(s1.id)

    s2 = LedgerSession(name="Still active")
    store.create_session(s2)

    active = store.list_sessions(status="active")
    assert len(active) == 1
    assert active[0]["name"] == "Still active"

    all_sessions = store.list_sessions(status=None)
    assert len(all_sessions) == 2


def test_rename_session(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="Old name")
    store.create_session(session)

    store.rename_session(session.id, "New name")
    loaded = store.load_session(session.id)
    assert loaded.name == "New name"


def test_update_session_description(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="Test")
    store.create_session(session)

    store.update_session_description(session.id, "Updated description")
    loaded = store.load_session(session.id)
    assert loaded.description == "Updated description"


def test_archive_session(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="To archive")
    store.create_session(session)

    store.archive_session(session.id)
    loaded = store.load_session(session.id)
    assert loaded.status == SessionStatus.ARCHIVED


def test_append_and_load_messages(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="Chat test")
    store.create_session(session)

    msg = ChatMessage(role="user", content="Hello", forge="test")
    store.append_message(session.id, msg)

    msg2 = ChatMessage(role="assistant", content="Hi there", forge="test")
    store.append_message(session.id, msg2)

    loaded = store.load_session(session.id, include_messages=True)
    assert len(loaded.chat_messages) == 2
    assert loaded.chat_messages[0].role == "user"
    assert loaded.chat_messages[1].role == "assistant"


def test_message_count(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="Count test")
    store.create_session(session)

    assert store.message_count(session.id) == 0

    store.append_message(session.id, ChatMessage(role="user", content="Hi"))
    store.append_message(session.id, ChatMessage(role="assistant", content="Hello"))
    assert store.message_count(session.id) == 2


def test_load_session_without_messages(tmp_db_path):
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="No messages")
    store.create_session(session)
    store.append_message(session.id, ChatMessage(role="user", content="Hi"))

    loaded = store.load_session(session.id, include_messages=False)
    assert len(loaded.chat_messages) == 0


def test_load_session_by_name(tmp_db_path):
    """Session lookup by unique name should work."""
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="my-test-session", description="Named session")
    store.create_session(session)

    loaded = store.load_session_by_name("my-test-session")
    assert loaded.id == session.id
    assert loaded.name == "my-test-session"


def test_load_session_by_name_not_found(tmp_db_path):
    """Lookup by non-existent name should raise KeyError."""
    store = LedgerStore(db_path=tmp_db_path)
    with pytest.raises(KeyError, match="not found"):
        store.load_session_by_name("non-existent-session")


def test_duplicate_session_name_rejected_on_create(tmp_db_path):
    """Duplicate session names should be rejected."""
    store = LedgerStore(db_path=tmp_db_path)
    s1 = LedgerSession(name="unique-name")
    store.create_session(s1)

    s2 = LedgerSession(name="unique-name")
    with pytest.raises(ValueError, match="already in use"):
        store.create_session(s2)


def test_duplicate_session_name_rejected_on_rename(tmp_db_path):
    """Renaming to an existing name should be rejected."""
    store = LedgerStore(db_path=tmp_db_path)
    s1 = LedgerSession(name="first")
    s2 = LedgerSession(name="second")
    store.create_session(s1)
    store.create_session(s2)

    with pytest.raises(ValueError, match="already in use"):
        store.rename_session(s2.id, "first")


def test_multiple_unnamed_sessions_allowed(tmp_db_path):
    """Multiple sessions without names should be allowed."""
    store = LedgerStore(db_path=tmp_db_path)
    s1 = LedgerSession(name="")  # unnamed
    s2 = LedgerSession(name="")  # also unnamed
    store.create_session(s1)
    store.create_session(s2)

    # Both should exist
    loaded1 = store.load_session(s1.id)
    loaded2 = store.load_session(s2.id)
    assert loaded1.name == ""
    assert loaded2.name == ""


def test_archived_session_excluded_from_name_lookup(tmp_db_path):
    """Archived sessions should not be found by name lookup."""
    store = LedgerStore(db_path=tmp_db_path)
    session = LedgerSession(name="to-be-archived")
    store.create_session(session)
    store.archive_session(session.id)

    with pytest.raises(KeyError, match="not found"):
        store.load_session_by_name("to-be-archived")


# ---------------------------------------------------------------------------
# extended coverage: migrations, caps, quarantine, rollback
# ---------------------------------------------------------------------------


def test_load_session_not_found_raises_keyerror(tmp_db_path):
    store = LedgerStore(tmp_db_path)
    with pytest.raises(KeyError, match="not found"):
        store.load_session("nonexistent")


def test_max_entries_cap_preserves_chronological_order(tmp_db_path):
    store = LedgerStore(tmp_db_path)
    s = LedgerSession(name="s")
    for i in range(5):
        e = LedgerEntry(kind=EntryKind.USER_NOTE, notes=f"n{i}")
        s.add(e)
    store.create_session(s)
    store.save_session(s)
    loaded = store.load_session(s.id, max_entries=2)
    # most recent 2, in chronological order
    assert [e.notes for e in loaded.entries] == ["n3", "n4"]


def test_max_messages_cap(tmp_db_path):
    store = LedgerStore(tmp_db_path)
    s = LedgerSession(name="s")
    store.create_session(s)
    for i in range(5):
        store.append_message(
            s.id,
            ChatMessage(role="user", content=f"m{i}"),
        )
    loaded = store.load_session(s.id, max_messages=2)
    assert [m.content for m in loaded.chat_messages] == ["m3", "m4"]


def test_corrupt_entry_row_quarantined(tmp_db_path):
    store = LedgerStore(tmp_db_path)
    s = LedgerSession(name="s")
    s.add(LedgerEntry(kind=EntryKind.USER_NOTE, notes="good"))
    store.create_session(s)
    store.save_session(s)
    # Inject a corrupt row directly
    store._conn.execute(
        "INSERT INTO entries (id, session_id, kind, timestamp, data) "
        "VALUES (?, ?, ?, ?, ?)",
        ("bad1", s.id, "user_note", "2024-01-01T00:00:00", "{not json"),
    )
    store._conn.commit()
    loaded = store.load_session(s.id)
    assert len(loaded.entries) == 1
    assert loaded.entries[0].notes == "good"


def test_append_entry_persists_and_updates(tmp_db_path):
    store = LedgerStore(tmp_db_path)
    s = LedgerSession(name="s")
    store.create_session(s)
    e = LedgerEntry(kind=EntryKind.TOOL_RUN, run_status="running")
    store.append_entry(s.id, e)
    # append_entry then update via save_session (ON CONFLICT upsert)
    e.run_status = "failed"
    s.entries = [e]
    store.save_session(s)
    loaded = store.load_session(s.id)
    assert loaded.entries[0].run_status == "failed"


def test_save_session_rollback_on_error(tmp_db_path, monkeypatch):
    store = LedgerStore(tmp_db_path)
    s = LedgerSession(name="s")
    store.create_session(s)

    def boom(session):
        raise RuntimeError("txn fail")

    monkeypatch.setattr(store, "_save_session_txn", boom)
    with pytest.raises(RuntimeError, match="txn fail"):
        store.save_session(s)


def test_schema_migration_applied(tmp_db_path, monkeypatch):
    import sqlite3

    import bioledger.ledger.store as store_mod

    # Pre-create a DB claiming to be one version behind with a no-op
    # migration registered
    store_mod._MIGRATIONS[store_mod.SCHEMA_VERSION] = (
        "CREATE TABLE IF NOT EXISTS _probe (id INTEGER);"
    )
    try:
        conn = sqlite3.connect(tmp_db_path)
        conn.executescript(store_mod._SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (store_mod.SCHEMA_VERSION - 1,),
        )
        conn.commit()
        conn.close()
        store = LedgerStore(tmp_db_path)
        # migration applied — probe table exists
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_probe'"
        ).fetchone()
        assert row is not None
    finally:
        store_mod._MIGRATIONS.pop(store_mod.SCHEMA_VERSION, None)


def test_missing_migration_raises(tmp_db_path):
    import sqlite3

    import bioledger.ledger.store as store_mod

    conn = sqlite3.connect(tmp_db_path)
    conn.executescript(store_mod._SCHEMA_V1)
    conn.execute("INSERT INTO schema_version (version) VALUES (-99)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="No migration"):
        LedgerStore(tmp_db_path)
