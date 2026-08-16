from __future__ import annotations

import hashlib
import logging
import shutil
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext

from bioledger.config import BioLedgerConfig
from bioledger.core.llm.agents import ForgeDeps, make_agent
from bioledger.core.engines.protocol import WorkflowHandle
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
from bioledger.ledger.models import (
    ContainerInfo,
    EntryKind,
    FileRef,
    LedgerEntry,
    LedgerSession,
)
from bioledger.ledger.store import LedgerStore
from bioledger.toolspec.models import ExecutionMode
from bioledger.toolspec.store import ToolStore


def _write_engine_metadata_sidecar(
    session_dir: Path,
    entry_id: str,
    run_handle: Any,
    module_dir: Path | None = None,
    tool_dir: Path | None = None,
) -> None:
    """Write _engine_metadata_{entry_id}.json sidecar at a predictable location.

    Stores all adapter-specific metadata needed for poll/collect/crystallize:
    - work_dir, run_id, executor, submitted_at, status
    - process_start_time (Snakemake/Nextflow)
    - planemo_pid (Galaxy, from run_handle.metadata)
    - module_dir (Nextflow discovered)
    - tool_dir (Galaxy discovered)
    """
    import json

    meta: dict[str, Any] = {
        "work_dir": str(run_handle.work_dir),
        "run_id": run_handle.run_id,
        "executor": run_handle.executor,
        "submitted_at": (
            run_handle.submitted_at.isoformat()
            if hasattr(run_handle.submitted_at, "isoformat")
            else str(run_handle.submitted_at)
        ),
        "status": run_handle.status,
    }
    if run_handle.process_start_time is not None:
        meta["process_start_time"] = run_handle.process_start_time
    if run_handle.metadata:
        if "planemo_pid" in run_handle.metadata:
            meta["planemo_pid"] = run_handle.metadata["planemo_pid"]
    if module_dir:
        meta["module_dir"] = str(module_dir)
    if tool_dir:
        meta["tool_dir"] = str(tool_dir)

    sidecar_path = session_dir / f"_engine_metadata_{entry_id}.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(meta, indent=2))


def _read_engine_metadata_sidecar(
    session_dir: Path, entry_id: str,
) -> dict[str, Any] | None:
    """Read the _engine_metadata sidecar, or None if it doesn't exist."""
    import json

    sidecar_path = session_dir / f"_engine_metadata_{entry_id}.json"
    if not sidecar_path.exists():
        return None
    try:
        return json.loads(sidecar_path.read_text())
    except Exception:
        return None


def _finalize_engine_entry(
    entry: LedgerEntry,
    run_handle: Any,
    work_dir: Path,
    adapter: Any,
    status: Any,
) -> LedgerEntry:
    """Finalize an engine entry after it reaches a terminal state.

    Calls adapter.collect() for engine-specific cleanup, discovers outputs
    from the work directory, creates FileRef objects, and persists status.
    """
    import hashlib as _hashlib

    from bioledger.forges.analysisforge.executor import _discover_outputs
    from bioledger.toolspec.models import ExecutionSpec, ToolSpec

    # 1. Call collect() for engine-specific cleanup/output download
    collected: list[dict] = []
    try:
        collected = adapter.collect(run_handle) or []
    except Exception as e:
        logging.warning("adapter.collect() failed for %s: %s", entry.id, e)

    # 2. Output discovery from work directory
    spec_snapshot = entry.tool_spec_snapshot or {}
    try:
        spec = ToolSpec(execution=ExecutionSpec(**spec_snapshot))
    except Exception:
        logging.warning(
            "Cannot reconstruct ToolSpec for entry %s, skipping output discovery",
            entry.id,
        )
        entry.run_status = status.state
        entry.exit_code = 0 if status.state == "completed" else 1
        if status.message:
            entry.notes = (
                (entry.notes + "\n" if entry.notes else "")
                + status.message
            )
        return entry

    input_mapping = entry.container.input_mapping if entry.container else {}
    staged_input_paths = {
        str((work_dir / name).resolve()) for name in input_mapping.values()
    }

    # Exclude known metadata files from output discovery
    exclude_paths: set[str] = set()
    for meta_file in (
        "_engine_metadata.json",
        "_pre_snapshot.json",
        "_process_start_time.json",
    ):
        meta_path = work_dir / meta_file
        if meta_path.exists():
            exclude_paths.add(str(meta_path.resolve()))
    # Also exclude the session-level sidecar
    sidecar_exclude = work_dir.parent / f"_engine_metadata_{entry.id}.json"
    if sidecar_exclude.exists():
        exclude_paths.add(str(sidecar_exclude.resolve()))

    output_paths = _discover_outputs(
        spec, work_dir, {}, staged_input_paths, exclude_paths=exclude_paths,
    )

    # Also include paths from collect() that aren't already discovered
    discovered_set = {str(p.resolve()) for p in output_paths}
    for item in collected:
        col_path = Path(item.get("path", ""))
        if col_path.exists() and str(col_path.resolve()) not in discovered_set:
            output_paths.append(col_path)

    # 3. Create FileRef objects for discovered outputs
    for out_path in output_paths:
        try:
            sha256 = _hashlib.sha256(out_path.read_bytes()).hexdigest()
            entry.files.append(
                FileRef(
                    path=str(out_path),
                    sha256=sha256,
                    size_bytes=out_path.stat().st_size,
                    role="output",
                )
            )
        except Exception as e:
            logging.warning("Failed to create FileRef for %s: %s", out_path, e)

    # 4. Persist status
    entry.run_status = status.state
    entry.exit_code = 0 if status.state == "completed" else 1
    if status.message:
        entry.notes = (
            (entry.notes + "\n" if entry.notes else "") + status.message
        )
    if status.state == "unknown":
        entry.notes = (
            (entry.notes + "\n" if entry.notes else "")
            + "Warning: engine status was uncertain (process gone)"
        )

    return entry


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
    engine: str | None = Field(
        default=None,
        description=(
            "Optional: specify a workflow engine for fan-out runs "
            "(nextflow/snakemake/galaxy). Only set when the user explicitly "
            "requests a specific engine or fan-out. Leave None for normal "
            "single-tool Docker runs."
        ),
    )
    discovered_source: str | None = Field(
        default=None,
        description=(
            "Optional: if the tool was discovered via tool search, "
            "set this to 'toolshed:<owner>/<repo>' or "
            "'nfcore:<subdirectory>' to trigger the discovered-tool "
            "execution path. Leave None for tools already in the local store."
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

        # Build engine availability info for the system prompt
        engine_status = self._build_engine_status()
        engines_default = config.engines.default if config.engines else "auto"

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
                f"Available engines: {engine_status}\n"
                f"Default engine: {engines_default}\n\n"
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
            _MAX_SNIPPETS = 10
            for entry in ctx.deps.session.entries:
                if entry.id == entry_id:
                    if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                        return f"Entry {entry_id} is a {entry.kind.value}, not a tool/script run."
                    output_files = [f for f in entry.files if f.role == "output"]
                    if not output_files:
                        return f"Entry {entry_id} ({entry.tool_spec_name}) has no output files."

                    snippets: list[str] = []
                    excluded_names: list[str] = []
                    for f in output_files[:_MAX_SNIPPETS]:
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

                    # Collect names of excluded files
                    if len(output_files) > _MAX_SNIPPETS:
                        excluded_names = [
                            Path(f.path).name
                            for f in output_files[_MAX_SNIPPETS:]
                        ]

                    header = f"Outputs for {entry.tool_spec_name} run {entry_id}:"
                    parts = [header, "\n\n".join(snippets)]

                    if excluded_names:
                        total = len(output_files)
                        shown = _MAX_SNIPPETS
                        excluded_count = total - shown
                        excluded_preview = ", ".join(excluded_names[:5])
                        if len(excluded_names) > 5:
                            excluded_preview += f", ... ({excluded_count - 5} more)"
                        parts.append(
                            f"\nShowing {shown} of {total} outputs. "
                            f"Excluded: {excluded_preview}"
                        )
                    return "\n\n".join(parts)
            return f"No entry found with ID '{entry_id}'."

        @self._chat_agent.tool
        def check_job_status(ctx: RunContext[ForgeDeps]) -> str:
            """Check the status of all background jobs in the session.
            Use this when the user asks about job status, running tasks,
            or whether something has finished. Returns a summary of every
            tool/script run entry with its current status."""
            _MAX_OUTPUTS = 5
            lines: list[str] = []
            for entry in ctx.deps.session.entries:
                if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                    output_files = [
                        Path(f.path).name for f in entry.files if f.role == "output"
                    ]
                    if len(output_files) > _MAX_OUTPUTS:
                        total = len(output_files)
                        shown = output_files[:_MAX_OUTPUTS]
                        outputs_str = (
                            f"{shown} ... ({total} total)"
                        )
                    else:
                        outputs_str = str(output_files)
                    lines.append(
                        f"  {entry.id} ({entry.tool_spec_name}): "
                        f"status={entry.run_status}, exit={entry.exit_code}, "
                        f"outputs={outputs_str}"
                    )
            if not lines:
                return "No tool/script runs in this session."
            return "Job statuses:\n" + "\n".join(lines)

        @self._chat_agent.tool
        def list_available_engines(ctx: RunContext[ForgeDeps]) -> str:
            """List which workflow engines are installed and available.
            Use this when the user asks about engine availability or
            whether a specific engine (nextflow/snakemake/galaxy) can
            be used for a fan-out or workflow run."""
            return self._build_engine_status()

        @self._chat_agent.tool
        def search_toolshed(
            ctx: RunContext[ForgeDeps], query: str
        ) -> str:
            """Search the Galaxy Tool Shed for tools matching the query.
            Use this when the user asks to find a tool that is not locally
            installed. Returns tool names, descriptions, and repo info.
            The user can then run a discovered tool by setting
            discovered_source='toolshed:<owner>/<repo>'."""
            try:
                from bioledger.core.engines.toolshed_search import (
                    search_toolshed as _search,
                )

                results = _search(query)
                if not results:
                    return f"No Tool Shed results for '{query}'."
                lines = []
                for r in results:
                    lines.append(
                        f"  {r.tool_id}: {r.name} — {r.description} "
                        f"(repo: {r.repo_owner}/{r.repo_name})"
                    )
                return "Tool Shed results:\n" + "\n".join(lines)
            except RuntimeError as e:
                return f"Tool Shed search unavailable: {e}"

        @self._chat_agent.tool
        def search_nfcore(
            ctx: RunContext[ForgeDeps], query: str
        ) -> str:
            """Search nf-core/modules for modules matching the query.
            Use this when the user asks to find an nf-core module that is
            not locally installed. Returns module names, subdirectories,
            and descriptions. The user can then run a discovered module by
            setting discovered_source='nfcore:<subdirectory>'."""
            try:
                from bioledger.core.engines.nfcore_search import (
                    search_nfcore as _search,
                )

                results = _search(query)
                if not results:
                    return f"No nf-core results for '{query}'."
                lines = []
                for r in results:
                    lines.append(
                        f"  {r.name} ({r.subdirectory}): {r.description}"
                    )
                return "nf-core results:\n" + "\n".join(lines)
            except RuntimeError as e:
                return f"nf-core search unavailable: {e}"

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
        # Capped at _MAX_PRIOR_OUTPUTS to avoid token blowup on fan-out runs.
        _MAX_PRIOR_OUTPUTS = 30
        prior_outputs: list[str] = []
        for entry in reversed(self.session.entries):
            if entry.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                output_files = [f for f in entry.files if f.role == "output"]
                if len(output_files) > 10:
                    # Cap per-entry outputs: show first few + count
                    shown = output_files[:5]
                    total = len(output_files)
                    names = ", ".join(Path(f.path).name for f in shown)
                    prior_outputs.append(
                        f"entry={entry.id} tool={entry.tool_spec_name} "
                        f"outputs={names} ... ({total} total)"
                    )
                else:
                    for f in output_files:
                        prior_outputs.append(
                            f"entry={entry.id} tool={entry.tool_spec_name} "
                            f"file={Path(f.path).name}"
                        )
                if len(prior_outputs) >= _MAX_PRIOR_OUTPUTS:
                    break

        # Available files from dataset
        dataset_files: list[str] = []
        if self.dataset:
            for f in self.dataset.files:
                loc = f.downloaded_path or f.location
                dataset_files.append(f"{Path(loc).name} (format: {f.format})")

        if len(prior_outputs) > _MAX_PRIOR_OUTPUTS:
            prior_outputs = prior_outputs[:_MAX_PRIOR_OUTPUTS]
            prior_outputs.append(
                f"... ({len(prior_outputs)} outputs shown, more available)"
            )
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
        and run_handle_id set. Call poll_pending_jobs() later to finalize it.
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

        Adapter-folding: branches on entry.executor to decide which
        adapter to poll. Docker/conda entries use the existing poll_tool()
        path; engine entries (snakemake/nextflow/galaxy) use the
        adapter's poll() method via dispatch().
        """
        from bioledger.core.engines.dispatch import _get_engine_adapter
        from bioledger.core.engines.protocol import RunHandle

        session_dir = get_session_dir(self.config.home_dir, self.session.id)
        newly_completed = []
        changed = False
        for entry in self.session.entries:
            if entry.run_status != "running":
                continue

            executor = entry.executor or "docker"
            if executor in ("docker", "conda"):
                # Existing Docker path — poll_tool finalizes in one step
                poll_tool(entry, session_dir)
            else:
                # Engine adapter path — use adapter.poll()
                try:
                    adapter = _get_engine_adapter(executor, self.config.engines)

                    # Reconstruct RunHandle from sidecar metadata
                    meta = _read_engine_metadata_sidecar(
                        session_dir, entry.id,
                    )
                    if not meta:
                        logging.warning(
                            "No engine metadata sidecar for entry %s — "
                            "cannot poll, marking as failed",
                            entry.id,
                        )
                        entry.run_status = "failed"
                        entry.notes = (
                            (entry.notes + "\n" if entry.notes else "")
                            + "Engine metadata sidecar lost — cannot poll"
                        )
                        newly_completed.append(entry)
                        changed = True
                        continue
                    rh = RunHandle(
                        executor=executor,
                        run_id=entry.run_handle_id or "",
                        entry_id=entry.id,
                        work_dir=Path(meta["work_dir"]),
                        metadata=(
                            {"planemo_pid": meta["planemo_pid"]}
                            if "planemo_pid" in meta else {}
                        ),
                        process_start_time=meta.get("process_start_time"),
                    )
                    status = adapter.poll(rh)
                    if status is not None and status.state in (
                        "completed", "failed", "unknown"
                    ):
                        _finalize_engine_entry(
                            entry, rh, rh.work_dir, adapter, status,
                        )
                except Exception as e:
                    logging.warning(
                        "Cannot poll entry %s with executor %s: %s",
                        entry.id, executor, e,
                    )

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
        """One-line-per-entry summary for LLM context.

        Output file listings are capped at _MAX_OUTPUTS per entry to avoid
        token blowup on fan-out runs (e.g. 500-gene scatter → one entry
        with 500 output files).
        """
        _MAX_OUTPUTS = 10
        lines = []
        for entry in self.session.entries:
            if entry.kind == EntryKind.TOOL_RUN:
                out_files = [
                    Path(f.path).name for f in entry.files if f.role == "output"
                ]
                if len(out_files) > _MAX_OUTPUTS:
                    total = len(out_files)
                    shown = ", ".join(out_files[:_MAX_OUTPUTS])
                    out_str = f"{shown} ... ({total} total)"
                else:
                    out_str = ", ".join(out_files)
                status = (
                    "running in background"
                    if entry.run_status == "running"
                    else f"exit={entry.exit_code}"
                )
                lines.append(
                    f"  [{entry.id}] {entry.tool_spec_name} -> "
                    f"{out_str} ({status})"
                )
            elif entry.kind == EntryKind.DATA_IMPORT:
                lines.append(f"  [{entry.id}] data_import: {entry.notes}")
        return "\n".join(lines) or "  (no entries yet)"

    def _build_engine_status(self) -> str:
        """Build a human-readable summary of installed workflow engines.

        Checks each engine's binary availability and Python package
        installation status. Used in the system prompt and by the
        list_available_engines agent tool.
        """
        statuses: list[str] = []
        for engine in ("nextflow", "snakemake", "galaxy"):
            binary_found = shutil.which(engine) is not None
            pkg_installed = False
            try:
                if engine == "snakemake":
                    import snakemake  # noqa: F401
                    pkg_installed = True
                elif engine == "galaxy":
                    import galaxy.tool_util  # noqa: F401
                    pkg_installed = True
            except ImportError:
                pass

            if binary_found or pkg_installed:
                parts = [engine, "installed"]
                if binary_found and not pkg_installed:
                    parts.append("(binary found)")
                elif not binary_found and pkg_installed:
                    parts.append("(package installed, binary not on PATH)")
                statuses.append(" ".join(parts))
            else:
                statuses.append(f"{engine} (not installed)")

        return ", ".join(statuses)

    def submit_fanout(
        self,
        tool_name: str,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        engine: str | None = None,
        parent_id: str | None = None,
    ) -> tuple[LedgerEntry, WorkflowHandle | None]:
        """Submit a fan-out workflow run via a workflow engine.

        Agent-driven fan-out path:
        1. Resolve the engine (explicit or auto-detect).
        2. Load the toolspec, dispatch to the engine adapter.
        3. Call generate() to produce a parameterized workflow.
        4. Create a LedgerEntry (run_status="running", executor=engine).
        5. Call submit() to bind values and launch.
        6. Return (entry, workflow_handle) for polling.

        The caller (agent/CLI) should show the planned fan-out to the
        user for confirmation before calling this method.

        Raises RuntimeError if no engine is available.
        Raises NotImplementedError if the engine adapter is not implemented.
        """
        from bioledger.core.engines.dispatch import (
            _get_engine_adapter,
            resolve_engine_for_fanout,
        )

        session_dir = get_session_dir(
            self.config.home_dir, self.session.id
        )

        # Resolve engine
        engines_cfg = self.config.engines if self.config else None
        resolved_engine = resolve_engine_for_fanout(engines_cfg, engine)

        # Load spec and get adapter
        spec = self.get_tool_spec(tool_name)
        adapter = _get_engine_adapter(resolved_engine, engines_cfg)

        # Generate the parameterized workflow
        handle = adapter.generate(
            spec=spec.execution,
            input_mapping=input_mapping,
        )

        # Create LedgerEntry for the fan-out run
        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name=tool_name,
            tool_spec_snapshot=spec.execution.model_dump(),
            container=ContainerInfo(
                image=handle.container_image or spec.execution.container,
                command=[],
                input_mapping=input_mapping,
            ),
            params=params,
            parent_id=parent_id,
            run_status="running",
            run_handle_id="",  # set after submit
            executor=resolved_engine,
            notes=f"Fan-out via {resolved_engine}: {handle.step_inventory}",
        )
        self.session.add(entry)

        # Submit the workflow
        run_handle = adapter.submit(
            handle=handle,
            input_mapping=input_mapping,
            params=params,
            work_dir=session_dir / f"fanout_{entry.id}",
        )

        # Write engine metadata sidecar for poll/collect reconstruction
        _write_engine_metadata_sidecar(
            session_dir, entry.id, run_handle,
        )

        # Update entry with run handle
        entry.run_handle_id = run_handle.run_id
        entry.executor_version = run_handle.metadata.get("engine_version", "")
        self.store.save_session(self.session)

        return entry, handle

    def submit_discovered(
        self,
        tool_name: str,
        discovered_source: str,
        input_mapping: dict[str, str],
        params: dict[str, Any],
        parent_id: str | None = None,
    ) -> tuple[LedgerEntry, Any]:
        """Submit a discovered tool (from Tool Shed or nf-core) for execution.

        Downloads the tool, parses it into an ExecutionSpec, then dispatches
        to the appropriate engine adapter via submit_spec().

        discovered_source format:
          - 'toolshed:<owner>/<repo>' — Galaxy Tool Shed
          - 'nfcore:<subdirectory>' — nf-core modules
        """
        from bioledger.core.engines.dispatch import _get_engine_adapter
        from bioledger.forges.analysisforge.executor import get_session_dir

        session_dir = get_session_dir(
            self.config.home_dir, self.session.id
        )

        # Parse the source string
        if discovered_source.startswith("toolshed:"):
            repo = discovered_source[len("toolshed:"):]
            owner, repo_name = repo.split("/", 1)
            from bioledger.core.engines.toolshed_search import (
                download_tool,
                parse_tool_to_spec,
            )

            work_dir = session_dir / f"discovered_{owner}_{repo_name}"
            tool_dir = download_tool(owner, repo_name, dest_dir=work_dir / "tool")
            spec = parse_tool_to_spec(tool_dir)
            engine = "galaxy"
        elif discovered_source.startswith("nfcore:"):
            subdir = discovered_source[len("nfcore:"):]
            from bioledger.core.engines.nfcore_search import (
                download_module,
                parse_meta_yml,
            )

            work_dir = session_dir / f"discovered_nfcore_{subdir.replace('/', '_')}"
            module_dir = download_module(subdir, dest_dir=work_dir / "module")
            meta = parse_meta_yml(module_dir)
            # Build a minimal ExecutionSpec from meta.yml data
            from bioledger.toolspec.models import (
                ExecutionSpec,
                ParamType,
                ToolInput,
                ToolOutput,
            )
            tool_name = subdir.replace("/", "_")
            inputs = {
                name: ToolInput(name=name, type=ParamType.FILE, format="any")
                for name in meta["inputs"]
            }
            outputs = {
                name: ToolOutput(
                    name=name, type=ParamType.FILE,
                    format="any", pattern=pattern,
                )
                for name, pattern in meta["outputs"].items()
            }
            first_input = meta["inputs"][0] if meta["inputs"] else "input"
            spec = ExecutionSpec(
                name=tool_name,
                version="",
                description="",
                container=meta["container"],
                command=f"// nf-core module — include from modules/{subdir}/main.nf\n"
                        f"// This process is a wrapper; the actual logic is in the module file.\n"
                        f"// Input: {{{{inputs.{first_input}}}}}",
                inputs=inputs,
                outputs=outputs,
                executors=["nextflow"],
            )
            engine = "nextflow"
        else:
            raise ValueError(
                f"Unknown discovered_source format: {discovered_source}"
            )

        # Get the engine adapter
        engines_cfg = self.config.engines if self.config else None
        adapter = _get_engine_adapter(engine, engines_cfg)

        # Create LedgerEntry
        entry = LedgerEntry(
            kind=EntryKind.TOOL_RUN,
            tool_spec_name=spec.name,
            tool_spec_snapshot=spec.model_dump(),
            container=ContainerInfo(
                image=spec.container,
                command=[],
                input_mapping=input_mapping,
            ),
            params=params,
            parent_id=parent_id,
            run_status="running",
            run_handle_id="",
            executor=engine,
            notes=f"Discovered tool from {discovered_source}",
        )
        self.session.add(entry)

        # Submit via submit_spec()
        submit_kwargs: dict[str, Any] = {
            "spec": spec,
            "input_mapping": input_mapping,
            "params": params,
            "work_dir": work_dir / f"run_{entry.id}",
        }
        # Pass tool_dir for Galaxy (Tool Shed) discovered tools
        if engine == "galaxy":
            submit_kwargs["tool_dir"] = tool_dir
        elif engine == "nextflow":
            submit_kwargs["module_dir"] = module_dir
        run_handle = adapter.submit_spec(**submit_kwargs)

        # Write engine metadata sidecar for poll/collect reconstruction
        _write_engine_metadata_sidecar(
            session_dir, entry.id, run_handle,
            module_dir=module_dir if engine == "nextflow" else None,
            tool_dir=tool_dir if engine == "galaxy" else None,
        )

        entry.run_handle_id = run_handle.run_id
        entry.executor_version = run_handle.metadata.get("engine_version", "")
        self.store.save_session(self.session)

        return entry, run_handle
