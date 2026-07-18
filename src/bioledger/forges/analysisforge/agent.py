from __future__ import annotations

import hashlib
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext

from bioledger.config import BioLedgerConfig
from bioledger.core.llm.agents import ForgeDeps, make_agent
from bioledger.forges.analysisforge.executor import (
    get_session_dir,
    poll_tool,
    run_tool,
    submit_tool,
)
from bioledger.forges.analysisforge.suggest import suggest_analysis_for_dataset
from bioledger.forges.isaforge.builder import csv_to_isatab
from bioledger.forges.isaforge.dataset import (
    DataSet,
    load_dataset_from_isatab,
)
from bioledger.forges.isaforge.download import download_remote_files
from bioledger.ledger.models import EntryKind, FileRef, LedgerEntry, LedgerSession
from bioledger.ledger.store import LedgerStore
from bioledger.toolspec.models import ExecutionMode
from bioledger.toolspec.store import ToolStore


class KeyValuePair(BaseModel):
    """A single key-value pair (used in place of dict for LLM compatibility)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str


class ToolRunRequest(BaseModel):
    """LLM-structured request to run a tool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(
        description="Name of the tool to run (must match an available tool)."
    )
    rationale: str = Field(
        description="Brief justification for choosing this tool."
    )
    suggested_params: list[KeyValuePair] = Field(
        default_factory=list,
        description=(
            "Parameters to pass to the tool. Each KeyValuePair has "
            "key=parameter_name, value=parameter_value."
        ),
    )
    input_mapping: list[KeyValuePair] = Field(
        default_factory=list,
        description=(
            "Maps each of the tool's declared inputs to a concrete source. "
            "Each KeyValuePair has key=<tool input name> and value=<source>. "
            "The source is resolved by the system in this order:\n"
            "  1. Absolute/relative file path that exists on disk.\n"
            "  2. Filename of a prior tool-run output in this session "
            "(e.g. 'summary.json'). Most recent match wins.\n"
            "  3. Filename, format, or substring from the loaded dataset "
            "(e.g. 's_study.txt' or 'fastq').\n"
            "  4. Filename inside the loaded ISA-Tab directory.\n"
            "When multiple prior runs produced a file with the same name "
            "and you need a specific one, prefix with the entry id: "
            "'<entry_id_prefix>/<filename>' (e.g. '4fccaa48/summary.json')."
        ),
    )

    def params_as_dict(self) -> dict[str, str]:
        """Convert suggested_params list back to a dict."""
        return {kv.key: kv.value for kv in self.suggested_params}

    def mapping_as_dict(self) -> dict[str, str]:
        """Convert input_mapping list back to a dict."""
        return {kv.key: kv.value for kv in self.input_mapping}


class ChatIntent(str, Enum):
    """What the LLM wants to do in response to the user's message."""

    RESPOND = "respond"  # pure conversational reply, no action
    SUGGEST_TOOL = "suggest_tool"  # LLM thinks a tool should be run next
    CLARIFY = "clarify"  # LLM needs more info before acting


class ChatResponse(BaseModel):
    """Structured output from the conversational chat agent.
    The intent field replaces fragile keyword scanning of free text."""

    intent: ChatIntent
    message: str  # the text shown to the user
    suggested_tool: str | None = None  # tool name, only when intent == SUGGEST_TOOL


class AnalysisForgeAgent:
    """Orchestrates the full interactive analysis session.

    Responsibilities:
    - Load ISA-Tab datasets (records DATA_IMPORT entry)
    - Suggest workflows and tools via LLM
    - Run tools with user confirmation (records TOOL_RUN entries)
    - Log all LLM interactions (records LLM_CALL entries + ChatMessages)
    - Provide entry review for selective RO-Crate packaging

    Design note — planned decomposition:
      This class currently handles dataset loading, tool suggestion, tool execution,
      and chat orchestration. Future refactoring should extract:
        1. DatasetManager — load_dataset, download handling, format detection
        2. ToolRunner — run_tool_with_logging, _resolve_inputs, _hash_file
        3. ChatOrchestrator — _analysis_chat loop, message recording
        4. AnalysisForgeAgent — thin coordinator delegating to the above
      This keeps each concern testable in isolation and reduces class size.
      The Forge base class (core/forge.py) provides the lifecycle contract.
    """

    def __init__(
        self, config: BioLedgerConfig, session: LedgerSession, store: LedgerStore
    ):
        self.config = config
        self.session = session
        self.store = store
        self.tool_store = ToolStore()
        self.dataset: DataSet | None = None

        # Initialize library client for auto-import
        from bioledger.core.library import ToolLibrary

        self._tool_library = ToolLibrary(cache_dir=config.home_dir / "cache")

        # Build dynamic tool list for the system prompt
        available_tools = self.tool_store.list_all()
        if available_tools:
            tools_list = "\n".join(
                f"- {t.name}: {t.execution.description or 'No description'}"
                for t in available_tools
            )
        else:
            tools_list = "(No tools locally imported yet)"

        # Include library tools so the LLM can suggest them for auto-import
        # Use list_cached() to avoid network fetch during construction
        library_entries = self._tool_library.list_cached()
        local_names = {t.name for t in available_tools}
        library_only = [e for e in library_entries if e["name"] not in local_names]
        if library_only:
            library_list = "\n".join(
                f"- {e['name']}: {e.get('description', '')}"
                for e in library_only
            )
        else:
            library_list = ""

        # Main conversational agent — structured output with explicit intent
        self._chat_agent = make_agent(
            config,
            task="chat",
            instructions=(
                "You are AnalysisForge, a bioinformatics analysis assistant. "
                "You help researchers analyze their data step by step. "
                "You can load ISA-Tab datasets, suggest analysis workflows, "
                "run bioinformatics tools, and package results into "
                "reproducible RO-Crates.\n\n"
                "When the user loads a dataset, summarize what you see and "
                "suggest a workflow. "
                "After each tool run, report results and suggest next steps. "
                "When the user asks about results from a tool run, use the "
                "fetch_entry_outputs tool to get the actual output — never guess. "
                "When the user asks about job status or whether a background "
                "job has finished, use the check_job_status tool to get "
                "accurate, real-time status for all jobs. "
                "When the user wants to package results, help them review and "
                "select entries.\n\n"
                f"Locally installed tools:\n{tools_list}\n\n"
                + (
                    f"Additional tools available from the library (auto-imported on use):\n"
                    f"{library_list}\n\n"
                    if library_list
                    else ""
                )
                + "IMPORTANT: When the user asks to run a tool, immediately set "
                "intent='suggest_tool' and suggested_tool=<name>. "
                "You may suggest tools from the library — they will be "
                "auto-imported when run. "
                "Do NOT ask the user to confirm — confirmation is handled "
                "separately by the system. "
                "Use intent='respond' for all other conversational replies. "
                "Use intent='clarify' when you need more info."
            ),
            output_type=ChatResponse,
        )

        # On-demand tool: LLM calls this to fetch output content for a
        # specific entry ID rather than having all outputs in context.
        @self._chat_agent.tool
        def fetch_entry_outputs(ctx: RunContext[ForgeDeps], entry_id: str) -> str:
            """Fetch output file contents for a tool/script run entry.
            Use this whenever the user asks about results from a specific run.
            Pass the entry ID (shown in review output or tool run messages)."""
            for entry in ctx.deps.session.entries:
                if entry.id == entry_id:
                    if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                        return f"Entry {entry_id} is a {entry.kind.value}, not a tool/script run."
                    snippets: list[str] = []
                    for f in entry.files:
                        if f.role == "output":
                            p = Path(f.path)
                            if p.exists() and p.stat().st_size < 50_000:
                                try:
                                    snippets.append(f"--- {p.name} ---\n{p.read_text()}")
                                except Exception:
                                    snippets.append(f"--- {p.name} --- (unreadable)")
                            elif p.exists():
                                size = p.stat().st_size
                                snippets.append(
                                    f"--- {p.name} --- ({size} bytes, too large to display)"
                                )
                            else:
                                snippets.append(f"--- {p.name} --- (file not found)")
                    if not snippets:
                        return f"Entry {entry_id} ({entry.tool_spec_name}) has no output files."
                    header = f"Outputs for {entry.tool_spec_name} run {entry_id}:"
                    return header + "\n\n" + "\n\n".join(snippets)
            return f"No entry found with ID '{entry_id}'."

        @self._chat_agent.tool
        def check_job_status(ctx: RunContext[ForgeDeps]) -> str:
            """Check the status of all background jobs in the session.
            Use this when the user asks about job status, running tasks,
            or whether something has finished. Returns a summary of every
            tool/script run entry with its current status."""
            lines: list[str] = []
            for entry in ctx.deps.session.entries:
                if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                    outputs = [
                        Path(f.path).name for f in entry.files if f.role == "output"
                    ]
                    lines.append(
                        f"  {entry.id} ({entry.tool_spec_name}): "
                        f"status={entry.run_status}, exit={entry.exit_code}, "
                        f"outputs={outputs}"
                    )
            if not lines:
                return "No tool/script runs in this session."
            return "Job statuses:\n" + "\n".join(lines)

        # Tool selection agent — structured output
        self._tool_select_agent = make_agent(
            config,
            task="analysis_suggest",
            instructions=(
                "You are a bioinformatics tool selection expert. Given a "
                "user's goal, the dataset context, and available tools, "
                "suggest which tool to run next with appropriate parameters. "
                "Return a structured ToolRunRequest."
            ),
            output_type=ToolRunRequest,
        )

    async def load_dataset(self, path: Path) -> DataSet:
        """Load a dataset into the session, record as DATA_IMPORT entry.

        Accepts either:
        - A CSV samplesheet file (.csv) — converted to ISA-Tab via ISAForge first
        - An ISA-Tab directory (containing i_investigation.txt)

        AnalysisForge always works with ISA-Tab internally for proper
        provenance and metadata tracking.
        """
        path = path.expanduser()
        # If path doesn't exist directly, try ~/.bioledger/datasets/<name>
        if not path.exists():
            candidate = self.config.home_dir / "datasets" / path.name
            if candidate.exists():
                path = candidate

        if path.is_file() and path.suffix.lower() == ".csv":
            # Route through ISAForge: CSV → ISA-Tab → load
            isatab_dir = (
                self.config.home_dir / "datasets" / path.stem
            )
            await csv_to_isatab(path, isatab_dir, config=self.config)
            dataset = load_dataset_from_isatab(isatab_dir, validate=False)
            source_desc = f"{path} (converted to ISA-Tab at {isatab_dir})"
        else:
            dataset = load_dataset_from_isatab(path)
            source_desc = str(path)
        self.dataset = dataset

        # Record DATA_IMPORT entry in ledger
        file_refs = []
        for f in dataset.files:
            if not f.is_remote:
                p = Path(f.location)
                if p.exists():
                    sha = hashlib.sha256(p.read_bytes()).hexdigest()
                    file_refs.append(
                        FileRef(
                            path=str(p),
                            sha256=sha,
                            size_bytes=p.stat().st_size,
                            role="input",
                        )
                    )

        entry = LedgerEntry(
            kind=EntryKind.DATA_IMPORT,
            files=file_refs,
            params={
                "source": source_desc,
                "organisms": dataset.organisms,
                "assay_type": dataset.assay_type,
                "file_formats": list(dataset.file_formats),
                "file_count": len(dataset.files),
                "remote_count": len(dataset.remote_files()),
            },
            notes=f"Loaded dataset: {dataset.name}",
        )
        self.session.add(entry)
        self.store.save_session(self.session)

        return dataset

    async def download_remote(self, download_dir: Path) -> DataSet:
        """Download remote files in the dataset (requires prior user confirmation)."""
        if not self.dataset:
            raise ValueError("No dataset loaded")
        self.dataset = await download_remote_files(
            self.dataset,
            download_dir,
            user_confirmed=True,
        )
        return self.dataset

    async def suggest_workflow(self, user_goal: str | None = None) -> dict:
        """Get LLM-powered workflow and tool suggestions for the loaded dataset."""
        if not self.dataset:
            raise ValueError("No dataset loaded — load ISA-Tab first")

        deps = ForgeDeps(
            config=self.config,
            session=self.session,
            store=self.store,
            context_mode="chat",
        )
        return await suggest_analysis_for_dataset(self.dataset, user_goal, deps)

    async def suggest_next_tool(self, user_message: str) -> ToolRunRequest:
        """LLM suggests the next tool to run based on conversation context."""
        deps = ForgeDeps(
            config=self.config,
            session=self.session,
            store=self.store,
            context_mode="chat",
        )

        available_tools = self.tool_store.list_all()

        # Build detailed tool info including input requirements
        tools_details = []
        for t in available_tools:
            inputs_info = ""
            if t.execution.inputs:
                inputs_info = "\n    Inputs: " + ", ".join(
                    f"{name} ({inp.format}, required={inp.required})"
                    for name, inp in t.execution.inputs.items()
                )
            tools_details.append(f"- {t.name}: {t.execution.description}{inputs_info}")
        tools_summary = "\n".join(tools_details)

        # Recent tool/script outputs with their entry IDs (most recent first).
        # The LLM uses these to chain tools: an output from run X can be an
        # input to a later tool. When multiple entries share a filename, the
        # LLM should disambiguate using '<entry_id>/<filename>' in the source.
        prior_outputs: list[str] = []
        for entry in reversed(self.session.entries):
            if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                for f in entry.files:
                    if f.role == "output":
                        prior_outputs.append(
                            f"entry={entry.id} tool={entry.tool_spec_name} "
                            f"file={Path(f.path).name}"
                        )

        # Available files from dataset
        dataset_files: list[str] = []
        if self.dataset:
            for f in self.dataset.files:
                loc = f.downloaded_path or f.location
                dataset_files.append(f"{Path(loc).name} (format: {f.format})")

        prior_outputs_text = (
            "\n".join(f"  - {o}" for o in prior_outputs)
            if prior_outputs
            else "  (none yet)"
        )
        context = (
            f"User request: {user_message}\n\n"
            f"Dataset: {self.dataset.name if self.dataset else 'none'}\n"
            f"Assay type: {self.dataset.assay_type if self.dataset else 'unknown'}\n"
            f"File formats: "
            f"{', '.join(self.dataset.file_formats) if self.dataset else 'unknown'}\n"
            f"Dataset files: {dataset_files if dataset_files else 'none loaded'}\n\n"
            f"Session history ({len(self.session.entries)} entries):\n"
            f"{self._session_summary()}\n\n"
            f"Prior tool-run outputs (available as inputs):\n"
            f"{prior_outputs_text}\n\n"
            f"Available tools:\n{tools_summary}\n"
        )
        result = await self._tool_select_agent.run(
            context, deps=deps, message_history=deps.message_history()
        )
        return result.output

    def get_tool_spec(self, tool_name: str) -> Any:
        """Load a tool spec, auto-importing from the library if needed.

        Shared by run_tool_with_logging() and callers (e.g. the CLI) that
        need to peek at spec details — like suggested_mode — before
        deciding how to run it.
        """
        if not self.tool_store.has(tool_name):
            lib_entry = self._tool_library.get(tool_name)
            if lib_entry:
                logging.info("Auto-importing tool '%s' from library", tool_name)
                self.tool_store.import_from_library(path=lib_entry["path"])
                logging.info("Auto-imported tool '%s'", tool_name)
        return self.tool_store.load(tool_name)

    async def run_tool_with_logging(
        self,
        tool_name: str,
        input_files: dict[str, Path],
        session_dir: Path,
        params: dict | None = None,
        parent_id: str | None = None,
        mode: ExecutionMode | None = None,
    ) -> tuple[LedgerEntry, Any]:
        """Run a tool and log everything to the session.

        If the tool is not locally available but exists in the library,
        it is auto-imported before execution.

        mode: None defers to the tool spec's `suggested_mode`. When the
        effective mode is ASYNC, submits the container and returns
        immediately with (entry, None) — the entry has run_status="running"
        and container_id set. Call poll_pending_jobs() later to finalize it.
        When BLOCKING, behaves as before: (entry, RunResult).
        """
        spec = self.get_tool_spec(tool_name)
        effective_mode = mode or spec.execution.suggested_mode

        if effective_mode == ExecutionMode.ASYNC:
            entry = submit_tool(
                self.session,
                spec,
                input_files,
                session_dir,
                params=params,
                parent_id=parent_id,
            )
            self.store.save_session(self.session)
            return entry, None

        try:
            entry, result = run_tool(
                self.session,
                spec,
                input_files,
                session_dir,
                params=params,
                parent_id=parent_id,
            )
        except TimeoutError:
            self.store.save_session(self.session)
            raise
        self.store.save_session(self.session)
        return entry, result

    def poll_pending_jobs(self) -> list[LedgerEntry]:
        """Check all running background jobs; finalize any that completed.

        Only scans entries with run_status == "running" (not the full
        history). Persists the session if anything changed. Returns the
        entries that just transitioned out of "running" this call.
        """
        session_dir = get_session_dir(self.config.home_dir, self.session.id)
        newly_completed = []
        changed = False
        for entry in self.session.entries:
            if entry.run_status != "running":
                continue
            poll_tool(entry, session_dir)
            if entry.run_status != "running":
                newly_completed.append(entry)
                changed = True
        if changed:
            self.store.save_session(self.session)
        return newly_completed

    def review_entries(self) -> list[dict]:
        """Return a summary of all session entries for user review."""
        summaries = []
        for entry in self.session.entries:
            inputs = [f.path for f in entry.files if f.role == "input"]
            outputs = [f.path for f in entry.files if f.role == "output"]

            # Build summary with all expected keys for consistent display
            summary = {
                "id": entry.id,
                "kind": entry.kind.value,
                "tool": entry.tool_spec_name or "",
                "timestamp": entry.timestamp.isoformat(),
                "inputs": inputs,
                "outputs": outputs,
                "params": entry.params,
                "exit_code": entry.exit_code,
                "run_status": entry.run_status,
                "notes": entry.notes or "",
                "parent_id": entry.parent_id,
            }

            # For data imports, use a more descriptive label
            if entry.kind == EntryKind.DATA_IMPORT and not summary["notes"]:
                summary["notes"] = entry.params.get("summary", "Loaded dataset")

            summaries.append(summary)
        return summaries

    def _session_summary(self) -> str:
        """One-line-per-entry summary for LLM context."""
        lines = []
        for entry in self.session.entries:
            if entry.kind == EntryKind.TOOL_RUN:
                out_files = [
                    Path(f.path).name for f in entry.files if f.role == "output"
                ]
                status = (
                    "running in background"
                    if entry.run_status == "running"
                    else f"exit={entry.exit_code}"
                )
                lines.append(
                    f"  [{entry.id}] {entry.tool_spec_name} -> "
                    f"{', '.join(out_files)} ({status})"
                )
            elif entry.kind == EntryKind.DATA_IMPORT:
                lines.append(f"  [{entry.id}] data_import: {entry.notes}")
        return "\n".join(lines) or "  (no entries yet)"
