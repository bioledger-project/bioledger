from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EntryKind(str, Enum):
    TOOL_RUN = "tool_run"  # ran a bioinformatics tool via container
    SCRIPT_RUN = "script_run"  # ran a custom script in a container
    LLM_CALL = "llm_call"  # LLM interaction (prompt + response)
    DATA_IMPORT = "data_import"  # user imported data files
    DATA_EXPORT = "data_export"  # user exported / downloaded results
    METADATA_GEN = "metadata_gen"  # ISA-Tab or other metadata generated
    USER_NOTE = "user_note"  # free-form annotation from the user


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatMessage(BaseModel):
    """A single user↔assistant message in a session's chat history.
    Stored separately from LedgerEntry — these drive LLM context,
    not provenance. Lightweight by design."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    forge: str = ""  # which forge produced this ("toolforge", "isaforge", etc.)
    entry_id: str | None = None  # optional link to the LedgerEntry this message relates to


class ContainerInfo(BaseModel):
    image: str  # e.g. "biocontainers/fastqc:0.12.1--hdfd78af_0"
    command: list[str]
    volumes: dict[str, str] = {}  # host_path → container_path
    env: dict[str, str] = {}
    input_mapping: dict[str, str] = {}  # tool_input_name → work-dir relative path


class LLMCallInfo(BaseModel):
    model: str  # e.g. "openai:gpt-4o"
    prompt_summary: str  # abbreviated prompt for display
    full_messages: list[dict[str, Any]]  # raw pydantic-ai messages
    output_summary: str
    tokens_used: int = 0
    tool_calls: list[str] = []  # names of tools the LLM invoked


class FileRef(BaseModel):
    path: str
    sha256: str
    size_bytes: int
    role: str = "input"  # "input" | "output" | "log" | "script"


class LedgerEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:20])
    kind: EntryKind
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    parent_id: str | None = None  # links to prior entry (DAG)
    tool_spec_name: str | None = None  # name of the ToolSpec used
    tool_spec_snapshot: dict[str, Any] | None = None  # frozen ExecutionSpec at time of run
    container: ContainerInfo | None = None
    llm_call: LLMCallInfo | None = None
    files: list[FileRef] = []
    params: dict[str, Any] = {}  # tool parameters, script args, etc.
    tags: list[str] = []
    notes: str = ""
    exit_code: int | None = None
    duration_seconds: float | None = None
    # Async tool-run tracking. Default "completed" preserves the meaning of
    # all pre-existing entries (no migration needed — entries are stored as
    # JSON blobs). "running" + run_handle_id set means the job was
    # submitted but not yet polled to completion; "queued" covers workflow-
    # engine queue states (e.g. Snakemake/Nextflow dry-run pending);
    # "unknown" covers the case where the job was lost (e.g. daemon restart,
    # externally removed). This 6-value set is a superset shaped to also
    # accommodate future HPC scheduler states (Slurm/LSF PENDING/RUNNING/
    # COMPLETED/FAILED/CANCELLED) without redesign.
    run_status: Literal[
        "pending", "queued", "running", "completed", "failed", "unknown"
    ] = "completed"
    run_handle_id: str | None = None  # set while run_status in ("queued", "running")
    # Which executor ran this entry. "docker" for single-tool Docker runs
    # (the existing path), "conda" for conda-based runs, or an engine name
    # ("nextflow"/"snakemake"/"galaxy") for workflow-engine submissions.
    executor: str = "docker"


class LedgerSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str = ""
    description: str = ""  # user-supplied description of the analysis
    status: SessionStatus = SessionStatus.ACTIVE
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    entries: list[LedgerEntry] = []
    chat_messages: list[ChatMessage] = []  # in-memory chat history for context

    def add(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        self.updated = datetime.now(timezone.utc)

    def add_message(
        self, role: str, content: str, forge: str = "", entry_id: str | None = None
    ) -> ChatMessage:
        """Append a chat message and return it."""
        msg = ChatMessage(role=role, content=content, forge=forge, entry_id=entry_id)
        self.chat_messages.append(msg)
        self.updated = datetime.now(timezone.utc)
        return msg

    def dag_edges(self) -> list[tuple[str, str]]:
        """Return parent→child edges for workflow reconstruction."""
        return [(e.parent_id, e.id) for e in self.entries if e.parent_id is not None]

    def dependency_graph(self) -> dict[str, set[str]]:
        """Return a file-lineage dependency graph: entry_id → set of producer entry_ids.

        Uses the same file-level matching as ``crystallize.to_nextflow`` —
        multi-parent, sha-aware. This is the canonical derived graph for
        the session, replacing ad-hoc recomputation in each consumer
        (crystallize, agent-context capping, RO-Crate builder).
        """
        from bioledger.forges.analysisforge.crystallize import (
            _build_dependency_graph,
            _build_output_source_map,
        )

        tool_entries = [
            e for e in self.entries
            if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
            and e.run_status in ("completed", "failed", "running")
        ]
        source_map = _build_output_source_map(tool_entries)
        return _build_dependency_graph(tool_entries, source_map)
