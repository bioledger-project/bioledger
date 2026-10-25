from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # Load .env into process environment before anything else

import typer  # noqa: E402
import docker.errors  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.markup import escape as rich_escape  # noqa: E402
from rich.table import Table  # noqa: E402

from bioledger.ledger.models import LedgerEntry, LedgerSession  # noqa: E402
from bioledger.ledger.store import LedgerStore  # noqa: E402

app = typer.Typer(name="bioledger", help="BioLedger: reproducible bio-analysis")
session_app = typer.Typer(help="Manage analysis sessions")
tool_app = typer.Typer(help="Manage tool specifications")
library_app = typer.Typer(help="Browse and import from the tool library")
study_app = typer.Typer(help="Browse and load ISA-Tab studies")
config_app = typer.Typer(help="Validate and inspect configuration")
app.add_typer(session_app, name="session")
app.add_typer(tool_app, name="tool")
app.add_typer(library_app, name="library")
app.add_typer(study_app, name="study")
app.add_typer(config_app, name="config")
console = Console()


# --- Session management ---


@session_app.command("new")
def session_new(
    name: str = typer.Option("", help="Session name (must be unique if provided)"),
    description: str = typer.Option("", help="What this analysis is about"),
) -> None:
    """Create a new analysis session."""
    session = LedgerSession(name=name, description=description)
    store = LedgerStore()
    try:
        store.create_session(session)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Session {session.id} created[/green]")
    if name:
        console.print(f"  Name: {name}")


@session_app.command("list")
def session_list(
    all_sessions: bool = typer.Option(
        False, "--all", help="Include archived sessions"
    ),
) -> None:
    """List analysis sessions."""
    store = LedgerStore()
    status = None if all_sessions else "active"
    rows = store.list_sessions(status=status)
    if not rows:
        console.print("[dim]No sessions found.[/dim]")
        return
    table = Table(title="Sessions")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Entries", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Updated")
    for r in rows:
        s = store.load_session(r["id"], include_messages=False)
        msg_count = store.message_count(r["id"])
        table.add_row(
            r["id"],
            r["name"] or "(unnamed)",
            r["status"],
            str(len(s.entries)),
            str(msg_count),
            r["updated"][:16],
        )
    console.print(table)


@session_app.command("show")
def session_show(session_id: str) -> None:
    """Show session details, entries, and recent chat."""
    store = LedgerStore()
    s = store.load_session(session_id)
    console.print(f"[bold]Session {s.id}[/bold]  {s.name or '(unnamed)'}")
    console.print(
        f"  Status: {s.status.value}  |  "
        f"Created: {s.created}  |  Updated: {s.updated}"
    )
    if s.description:
        console.print(f"  Description: {s.description}")
    console.print(
        f"  Entries: {len(s.entries)}  |  "
        f"Chat messages: {len(s.chat_messages)}"
    )
    # Show last 5 chat messages
    if s.chat_messages:
        console.print("\n[bold]Recent chat:[/bold]")
        for msg in s.chat_messages[-5:]:
            role_color = "green" if msg.role == "user" else "blue"
            console.print(
                f"  [{role_color}]{msg.role}[/{role_color}]: "
                f"{msg.content[:120]}"
            )


@session_app.command("rename")
def session_rename(session_id: str, name: str) -> None:
    """Rename a session."""
    store = LedgerStore()
    try:
        store.rename_session(session_id, name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Session {session_id} renamed to '{name}'[/green]")


@session_app.command("describe")
def session_describe(session_id: str, description: str) -> None:
    """Update a session's description."""
    store = LedgerStore()
    store.update_session_description(session_id, description)
    console.print("[green]Description updated[/green]")


@session_app.command("archive")
def session_archive(session_id: str) -> None:
    """Archive a session (soft-delete, still queryable)."""
    store = LedgerStore()
    store.archive_session(session_id)
    console.print(f"[yellow]Session {session_id} archived[/yellow]")


# --- Tool management ---


@tool_app.command("import")
def tool_import(
    path: Path,
    name: str = typer.Option("", help="Override tool name"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Render the resulting spec to stdout without saving to the store",
    ),
) -> None:
    """Import a tool from a BioLedger YAML spec file."""
    from bioledger.toolspec.load import load_spec

    suffix = path.suffix.lower()

    if suffix in (".yaml", ".yml"):
        spec = load_spec(path)
    else:
        console.print(f"[red]Unsupported file type: {suffix}. Only .yaml/.yml is supported.[/red]")
        raise typer.Exit(1)

    if name:
        spec.execution.name = name

    _finalize_tool_import(spec, dry_run=dry_run)


def _finalize_tool_import(spec, *, dry_run: bool) -> None:
    """Validate, then either render YAML to stdout (dry-run) or save to the store."""
    from bioledger.apps.cli._ui import print_validation_issues
    from bioledger.toolspec.load import dump_spec_yaml
    from bioledger.toolspec.store import ToolStore
    from bioledger.toolspec.validate import validate_spec

    result = validate_spec(spec)

    if dry_run:
        console.print(
            f"[cyan]Dry run — would import '{spec.name}' "
            "(no files written)[/cyan]"
        )
        console.print(dump_spec_yaml(spec))
    else:
        store = ToolStore()
        out = store.save(spec)
        console.print(f"[green]Imported '{spec.name}' → {out}[/green]")

    if result.issues:
        print_validation_issues(result, console)


@tool_app.command("validate")
def tool_validate(
    path: Path,
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors"),
) -> None:
    """Validate a tool spec file."""
    from bioledger.apps.cli._ui import print_validation_issues
    from bioledger.toolspec.load import load_spec
    from bioledger.toolspec.validate import validate_spec

    spec = load_spec(path)
    result = validate_spec(spec, strict=strict)

    if result.is_valid and (not strict or result.is_strict_valid):
        console.print(f"[green]✓ {spec.name} is valid[/green]")
    else:
        console.print(f"[red]✗ {spec.name} has issues[/red]")

    print_validation_issues(result, console, show_field=True)

    if not result.is_valid:
        raise typer.Exit(1)


@tool_app.command("list")
def tool_list(
    search: str = typer.Option("", help="Filter by name substring"),
) -> None:
    """List tool specs in the local store."""
    from bioledger.toolspec.store import ToolStore

    store = ToolStore()
    specs = store.search(name=search) if search else store.list_all()

    if not specs:
        console.print("[dim]No tools found.[/dim]")
        return

    table = Table(title="Tool Specs")
    table.add_column("Name", style="cyan")
    table.add_column("Container")
    table.add_column("Status")
    table.add_column("Inputs", justify="right")
    table.add_column("Outputs", justify="right")

    for spec in specs:
        ex = spec.execution
        table.add_row(
            ex.name,
            ex.container,
            ex.status.value,
            str(len(ex.inputs)),
            str(len(ex.outputs)),
        )
    console.print(table)


@tool_app.command("show")
def tool_show(name: str) -> None:
    """Show details of a tool spec."""
    from bioledger.toolspec.store import ToolStore

    store = ToolStore()
    try:
        spec = store.load(name)
    except KeyError:
        console.print(f"[red]Tool '{name}' not found[/red]")
        raise typer.Exit(1)

    ex = spec.execution
    console.print(f"[bold]{ex.name}[/bold]  v{ex.version or '(unset)'}")
    console.print(f"  Container: {ex.container}")
    console.print(f"  Status:    {ex.status.value}")
    console.print(f"  Command:   {ex.command}")

    if ex.description:
        console.print(f"  Desc:      {ex.description}")

    if ex.inputs:
        console.print("\n  [bold]Inputs:[/bold]")
        for name, inp in ex.inputs.items():
            req = "required" if inp.required else "optional"
            console.print(f"    {name}: {inp.format} ({req})")

    if ex.outputs:
        console.print("\n  [bold]Outputs:[/bold]")
        for name, out in ex.outputs.items():
            console.print(f"    {name}: {out.format}")

    if ex.parameters:
        console.print("\n  [bold]Parameters:[/bold]")
        for name, param in ex.parameters.items():
            default = f" = {param.default}" if param.default is not None else ""
            console.print(f"    {name}: {param.type.value}{default}")


@tool_app.command("search")
def tool_search(
    query: str = typer.Argument(..., help="Search query (tool name or keyword)"),
    source: str = typer.Option(
        "all", "--source", help="Search source: 'toolshed', 'nfcore', or 'all'"
    ),
) -> None:
    """Search Galaxy Tool Shed and/or nf-core modules for tools."""
    if source in ("toolshed", "all"):
        console.print("\n[bold cyan]Galaxy Tool Shed results:[/bold cyan]")
        try:
            from bioledger.core.engines.toolshed_search import search_toolshed

            results = search_toolshed(query)
            if results:
                table = Table(title="Tool Shed")
                table.add_column("Tool ID", style="cyan")
                table.add_column("Name")
                table.add_column("Description")
                table.add_column("Repo")
                for r in results:
                    table.add_row(
                        r.tool_id,
                        r.name,
                        r.description[:60] if r.description else "",
                        f"{r.repo_owner}/{r.repo_name}",
                    )
                console.print(table)
            else:
                console.print("[dim]No results.[/dim]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")

    if source in ("nfcore", "all"):
        console.print("\n[bold cyan]nf-core modules results:[/bold cyan]")
        try:
            from bioledger.core.engines.nfcore_search import search_nfcore

            results = search_nfcore(query)
            if results:
                table = Table(title="nf-core modules")
                table.add_column("Name", style="cyan")
                table.add_column("Subdirectory")
                table.add_column("Description")
                for r in results:
                    table.add_row(
                        r.name,
                        r.subdirectory,
                        r.description[:60] if r.description else "",
                    )
                console.print(table)
            else:
                console.print("[dim]No results.[/dim]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")


# --- Analysis ---


@app.command()
def resume(session_ref: str) -> None:
    """Resume an interactive analysis session (chat mode).

    Accepts either a session ID or a unique session name.
    """
    store = LedgerStore()
    # Try ID first, fall back to name lookup
    try:
        session = store.load_session(session_ref)
    except KeyError:
        try:
            session = store.load_session_by_name(session_ref)
        except KeyError:
            console.print(f"[red]Session '{session_ref}' not found by ID or name[/red]")
            raise typer.Exit(1)
    # Pass the resolved session ID to the chat handler
    asyncio.run(_analysis_chat(session.id))


async def _analysis_chat(session_id: str) -> None:
    """Interactive chat loop for AnalysisForge.

    Handles: dataset loading, LLM-powered tool suggestions, tool execution with
    user confirmation, entry review, and selective RO-Crate packaging.
    """
    from bioledger.config import BioLedgerConfig
    from bioledger.core.llm.agents import ForgeDeps
    from bioledger.forges.analysisforge.agent import (
        AnalysisForgeAgent,
        ChatIntent,
    )
    from bioledger.forges.isaforge.dataset import load_dataset_from_isatab
    from bioledger.ledger.models import EntryKind

    config = BioLedgerConfig()
    store = LedgerStore()
    session = store.load_session(session_id, include_messages=True)
    agent = AnalysisForgeAgent(config, session, store)

    # Restore dataset from prior DATA_IMPORT entry (if any)
    for entry in session.entries:
        if entry.kind == EntryKind.DATA_IMPORT and "source" in entry.params:
            source = entry.params["source"]
            # Strip conversion suffix if present (e.g. "path (converted to ISA-Tab at ...)")
            if " (converted to ISA-Tab at " in source:
                isatab_path = source.split("(converted to ISA-Tab at ")[-1].rstrip(")")
            else:
                isatab_path = source
            try:
                dataset = load_dataset_from_isatab(Path(isatab_path), validate=False)
                agent.dataset = dataset
            except Exception:
                pass  # dataset dir may have moved; user can re-load

    console.print(f"\n[bold]Session: {session.name or session.id}[/bold]")
    console.print(
        f"  {len(session.entries)} entries, "
        f"{len(session.chat_messages)} messages"
    )
    console.print(
        "[dim]Type 'quit' to exit, 'review' to see entries, "
        "'status' to check background jobs, "
        "'package' to build RO-Crate[/dim]\n"
    )

    deps = ForgeDeps(
        config=config,
        session=session,
        store=store,
        context_mode="chat",
    )

    while True:
        try:
            for entry in agent.poll_pending_jobs():
                _print_job_completion(console, entry)
                # Record the completion in chat history so the LLM knows
                # about it when the user asks about job status later.
                outputs = [Path(f.path).name for f in entry.files if f.role == "output"]
                completion_msg = (
                    f"Background job {entry.id} ({entry.tool_spec_name}) "
                    f"finished with status: {entry.run_status}"
                    f" (exit {entry.exit_code}). Outputs: {outputs}"
                )
                session.add_message("assistant", completion_msg, forge="analysisforge")
                store.append_message(session.id, session.chat_messages[-1])
        except (ConnectionError, OSError):
            pass
        except docker.errors.DockerException:
            pass

        try:
            user_input = console.input("[green]you>[/green] ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break

        if user_input.lower() == "status":
            _print_job_status(console, agent.session)
            continue

        # Record user message
        session.add_message("user", user_input, forge="analysisforge")
        store.append_message(session.id, session.chat_messages[-1])

        # --- Special commands ---

        if user_input.lower() == "review":
            entries = agent.review_entries()
            for e in entries:
                kind = e["kind"]
                icon = (
                    "[yellow]TOOL[/yellow]"
                    if kind == "tool_run"
                    else "[cyan]DATA[/cyan]"
                    if kind == "data_import"
                    else "[dim]NOTE[/dim]"
                )
                entry_id = e.get("id") or "[red]NO-ID[/red]"
                label = e.get("tool") or e.get("notes", "")
                outputs = e.get("outputs", [])
                output_names = [Path(p).name for p in outputs]
                console.print(
                    f"  {icon} ({entry_id}) {kind}: {rich_escape(label)}  "
                    f"outputs={output_names}"
                )
            continue

        if user_input.lower().startswith("package"):
            console.print("\n[bold]Package session into RO-Crate[/bold]")
            entries = agent.review_entries()

            console.print(
                "Select entries to include (comma-separated IDs, or 'all'):"
            )
            for e in entries:
                if e["kind"] in ("tool_run", "script_run"):
                    entry_id = e.get("id") or "[red]NO-ID[/red]"
                    tool_name = e.get("tool", "unknown")
                    outputs = e.get("outputs", [])
                    output_names = [Path(p).name for p in outputs]
                    console.print(
                        f"  ({entry_id}) {rich_escape(tool_name)} -> {output_names}"
                    )

            selection = console.input("[green]entries>[/green] ").strip()

            if selection.lower() == "all":
                entry_ids = None
            else:
                entry_ids = [eid.strip() for eid in selection.split(",")]

            from bioledger.forges.crateforge.builder import build_rocrate
            from bioledger.forges.analysisforge.executor import get_session_dir

            output_dir = config.home_dir / "crates" / session.id
            session_dir = get_session_dir(config.home_dir, session.id)
            crate_dir = build_rocrate(
                session, output_dir, entry_ids=entry_ids,
                session_dir=session_dir,
            )
            console.print(f"[green]RO-Crate written to {crate_dir}[/green]")

            response = (
                f"Packaged {'selected entries' if entry_ids else 'all entries'} "
                f"into RO-Crate at {crate_dir}"
            )
            session.add_message("assistant", response, forge="analysisforge")
            store.append_message(session.id, session.chat_messages[-1])
            continue

        if user_input.lower().startswith("load "):
            data_path = Path(user_input.split(" ", 1)[1].strip()).expanduser()
            try:
                dataset = await agent.load_dataset(data_path)

                # Show dataset summary
                samples = len(dataset.sample_metadata)
                orgs = ", ".join(dataset.organisms) if dataset.organisms else "unknown"
                fmts = ", ".join(dataset.file_formats) if dataset.file_formats else "none"
                console.print(
                    f'\n[green]Loaded dataset "{dataset.name}"[/green]'
                    f"\n  Samples: {samples}"
                    f"\n  Organisms: {orgs}"
                    f"\n  File formats: {fmts}"
                    f"\n  Files: {len(dataset.files)}"
                )

                # Check for remote files
                remote = dataset.remote_files()
                if remote:
                    console.print(
                        f"\n[yellow]Found {len(remote)} remote files:[/yellow]"
                    )
                    for f in remote:
                        console.print(f"  - {f.location}")
                    if typer.confirm("Download these files?"):
                        download_dir = (
                            config.home_dir / "datasets" / dataset.name
                        )
                        await agent.download_remote(download_dir)
                        console.print(
                            f"[green]Downloaded to {download_dir}[/green]"
                        )

                # Suggest workflow
                suggestions = await agent.suggest_workflow()
                response = suggestions["prompt_for_user"]
                console.print(f"\n[blue]assistant>[/blue] {response}")

                if suggestions.get("workflow"):
                    console.print("\n[bold]Suggested workflow:[/bold]")
                    for i, step in enumerate(suggestions["workflow"], 1):
                        tools = suggestions.get("tools_by_step", {}).get(
                            step, []
                        )
                        tools_str = f" ({', '.join(tools)})" if tools else ""
                        console.print(f"  {i}. {step}{tools_str}")

            except Exception as e:
                response = f"Failed to load dataset: {e}"
                console.print(f"[red]{response}[/red]")

            session.add_message("assistant", response, forge="analysisforge")
            store.append_message(session.id, session.chat_messages[-1])
            continue

        # --- General conversation: LLM decides what to do ---

        result = await agent._chat_agent.run(
            user_input, deps=deps, message_history=deps.message_history()
        )
        chat_response = result.output
        response = chat_response.message

        if chat_response.intent == ChatIntent.SUGGEST_TOOL:
            try:
                tool_request = await agent.suggest_next_tool(user_input)
                console.print(
                    f"\n[yellow]Suggested: {tool_request.tool_name}[/yellow]"
                    f"\n  Reason: {tool_request.rationale}"
                    f"\n  Params: {tool_request.params_as_dict()}"
                )

                if typer.confirm("Run this tool?"):
                    input_files, parent_id = _resolve_inputs(
                        agent, tool_request, user_input
                    )
                    from bioledger.forges.analysisforge.executor import (
                        get_session_dir,
                    )
                    from bioledger.toolspec.models import ExecutionMode

                    session_dir = get_session_dir(config.home_dir, session.id)

                    # Discovered-tool path: if the LLM set discovered_source,
                    # download the tool and run via submit_spec() on the
                    # appropriate engine.
                    if tool_request.discovered_source:
                        try:
                            entry, handle = agent.submit_discovered(
                                tool_name=tool_request.tool_name,
                                discovered_source=tool_request.discovered_source,
                                input_mapping={
                                    k: str(v) for k, v in input_files.items()
                                },
                                params=tool_request.params_as_dict(),
                                parent_id=parent_id,
                            )
                            response = (
                                f"Submitted discovered tool "
                                f"{tool_request.tool_name} as job {entry.id}. "
                                f"Source: {tool_request.discovered_source}. "
                                f"Type 'status' to check progress."
                            )
                            console.print(f"\n[blue]assistant>[/blue] {response}")
                        except Exception as e:
                            response = (
                                f"Failed to run discovered tool: {e}"
                            )
                            console.print(f"\n[red]{response}[/red]")
                    # Fan-out path: if the LLM requested an engine, use
                    # submit_fanout instead of the normal Docker path.
                    elif tool_request.engine:
                        try:
                            entry, handle = agent.submit_fanout(
                                tool_name=tool_request.tool_name,
                                input_mapping={
                                    k: str(v) for k, v in input_files.items()
                                },
                                params=tool_request.params_as_dict(),
                                engine=tool_request.engine,
                                parent_id=parent_id,
                            )
                            response = (
                                f"Submitted fan-out via {tool_request.engine}: "
                                f"{tool_request.tool_name} as job {entry.id}. "
                                f"Scattering over collection inputs. "
                                f"Type 'status' to check progress."
                            )
                            console.print(f"\n[blue]assistant>[/blue] {response}")
                        except NotImplementedError as e:
                            response = (
                                f"Engine '{tool_request.engine}' is not yet "
                                f"implemented. {e}\n"
                                f"Available engines: check 'list_available_engines' "
                                f"or install with: pip install bioledger[{tool_request.engine}]"
                            )
                            console.print(f"\n[red]{response}[/red]")
                        except RuntimeError as e:
                            response = (
                                f"Cannot run fan-out: {e}\n"
                                f"Fan-out requires a workflow engine. "
                                f"Install one with: "
                                f"pip install bioledger[snakemake]"
                            )
                            console.print(f"\n[red]{response}[/red]")
                    else:
                        # Peek the spec's suggested_mode to default the prompt
                        # sensibly; the user can always override either way.
                        try:
                            suggested_mode = agent.get_tool_spec(
                                tool_request.tool_name
                            ).execution.suggested_mode
                        except Exception:
                            suggested_mode = ExecutionMode.BLOCKING

                        run_in_background = typer.confirm(
                            "Run in background?",
                            default=(suggested_mode == ExecutionMode.ASYNC),
                        )
                        mode = (
                            ExecutionMode.ASYNC
                            if run_in_background
                            else ExecutionMode.BLOCKING
                        )

                        entry, run_result = await agent.run_tool_with_logging(
                            tool_request.tool_name,
                            input_files,
                            session_dir,
                            params=tool_request.params_as_dict(),
                            parent_id=parent_id,
                            mode=mode,
                        )

                        if run_result is None:
                            # Submitted as a background job — nothing to report yet.
                            response = (
                                f"Submitted {tool_request.tool_name} as job "
                                f"{entry.id}. I'll let you know when it finishes "
                                "(or type 'status' to check on it)."
                            )
                            console.print(f"\n[blue]assistant>[/blue] {response}")
                        elif run_result.exit_code == 0:
                            outputs = [
                                Path(f.path).name
                                for f in entry.files
                                if f.role == "output"
                            ]
                            # Read small output files so the LLM can discuss results
                            output_snippets = []
                            for f in entry.files:
                                if f.role == "output":
                                    op = Path(f.path)
                                    if op.exists() and op.stat().st_size < 10_000:
                                        try:
                                            output_snippets.append(
                                                f"--- {op.name} ---\n{op.read_text()}"
                                            )
                                        except Exception:
                                            pass
                            response = (
                                f"{tool_request.tool_name} completed. "
                                f"Outputs: {outputs}"
                            )
                            if output_snippets:
                                response += "\n" + "\n".join(output_snippets)
                            console.print(f"\n[blue]assistant>[/blue] {response}")
                        else:
                            run_dir_guess = (
                                Path(entry.files[0].path).parent
                                if entry.files
                                else session_dir / "runs" / entry.id
                            )
                            snippet = _failure_snippet(run_result, run_dir_guess)
                            fail_msg = (
                                f"{tool_request.tool_name} failed "
                                f"(exit {run_result.exit_code}):\n{snippet}"
                            )
                            console.print(f"\n[red]{fail_msg}[/red]")
                            # Ask the LLM to diagnose and suggest next steps
                            diagnosis = await agent._chat_agent.run(
                                f"The tool '{tool_request.tool_name}' just failed with this error:\n"
                                f"{snippet}\n\n"
                                "Diagnose the problem in one sentence and suggest the specific "
                                "next step the user should take to fix it (e.g. run a different "
                                "tool first, change a parameter, use a different file format).",
                                deps=deps,
                                message_history=deps.message_history(),
                            )
                            response = fail_msg + "\n\n" + diagnosis.output.message
                            console.print(f"\n[blue]assistant>[/blue] {diagnosis.output.message}")
                else:
                    response = (
                        "OK, skipping tool run. What would you like to do "
                        "instead?"
                    )
                    console.print(f"\n[blue]assistant>[/blue] {response}")
            except KeyError as e:
                response = f"Tool not found in store: {e}"
                console.print(f"[red]{response}[/red]")
            except Exception as e:
                response = f"Error suggesting/running tool: {e}"
                console.print(f"[red]{response}[/red]")
        else:
            # RESPOND or CLARIFY — just show the message
            console.print(f"\n[blue]assistant>[/blue] {response}")

        # Record assistant response
        session.add_message("assistant", response, forge="analysisforge")
        store.append_message(session.id, session.chat_messages[-1])


def _failure_snippet(result: Any, run_dir: Path, max_chars: int = 3000) -> str:
    """Return a tail-biased snippet of tool output for console display and LLM diagnosis.

    Tools like GATK/Picard emit verbose startup banners at the start of stderr
    and put the actual error near the end. A head-slice would hide the real
    failure message, so we bias toward the tail.
    """
    text = result.stderr or ""
    source = "stderr"
    if not text.strip():
        text = result.stdout or ""
        source = "stdout"
    if not text:
        return "(no output captured)"

    if len(text) <= max_chars:
        return rich_escape(text)

    tail = text[-max_chars:]
    return (
        f"[showing last {max_chars} chars of {source} — "
        f"full log: {run_dir / source}.log]\n{rich_escape(tail)}"
    )


def _print_job_completion(console: Console, entry: LedgerEntry) -> None:
    """Print a completion notice for a background job that just finished
    (detected via the auto-poll at the top of the chat loop)."""
    outputs = [Path(f.path).name for f in entry.files if f.role == "output"]
    if entry.run_status == "completed":
        console.print(
            f"\n[green]Job finished:[/green] {entry.id} ({entry.tool_spec_name})"
            f"\n  Outputs: {outputs}"
        )
        return

    console.print(
        f"\n[red]Job {entry.run_status} (exit {entry.exit_code}):[/red] "
        f"{entry.id} ({entry.tool_spec_name})"
    )
    stderr_path = next(
        (f.path for f in entry.files if f.role == "log" and f.path.endswith("stderr.log")),
        None,
    )
    if stderr_path and Path(stderr_path).exists():
        text = Path(stderr_path).read_text()
        if text.strip():
            snippet = text[-3000:] if len(text) > 3000 else text
            console.print(
                f"[showing last {len(snippet)} chars of stderr — "
                f"full log: {stderr_path}]\n{rich_escape(snippet)}"
            )


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    return f"{hours}h {mins}m"


def _print_job_status(console: Console, session: LedgerSession) -> None:
    """List currently running background jobs with elapsed time."""
    from datetime import datetime, timezone

    running = [e for e in session.entries if e.run_status == "running"]
    if not running:
        console.print("[dim]No background jobs running.[/dim]")
        return
    now = datetime.now(timezone.utc)
    for e in running:
        elapsed = (now - e.timestamp).total_seconds()
        # Note: entry IDs are printed in parens, not brackets — Rich
        # interprets "[...]" as markup and would silently swallow them.
        console.print(f"  ({e.id}) {e.tool_spec_name} — running for {_format_elapsed(elapsed)}")


def _resolve_inputs(
    agent: "AnalysisForgeAgent",  # noqa: F821
    tool_request: "ToolRunRequest",  # noqa: F821
    user_input: str = "",
) -> tuple[dict[str, Path], str | None]:
    """Resolve input file paths from tool request mapping.

    Resolution is done in two phases so that an EXACT filename match from
    any source always wins over a fuzzy (substring/format) match from any
    source. This matters because derived sidecar files (e.g. a samtools
    '<ref>.dict') often contain an earlier file's exact name as a
    substring — without this phase ordering, such a sidecar can shadow the
    real file it was derived from.

    Phase 1 (exact name only), in priority order:
      1. Literal file path (if exists on disk)
      2. Prior session tool/script output whose filename matches exactly
      3. Dataset file whose filename matches exactly
      4. ISA-Tab directory structural file (already exact by construction)
      5. ~/.bioledger/datasets/ recursive search (already exact by construction)

    Phase 2 (fuzzy fallback, only if nothing matched exactly above):
      6. Prior session output matched by substring
      7. Prior session output matched by declared input format (extension)
      8. Dataset file matched by substring or format

    Returns:
        (input_files, parent_id) — parent_id is set only when an input
        was resolved from a prior tool run's output (real data dependency).

    Raises ValueError if any required input cannot be resolved.
    """
    from bioledger.ledger.models import EntryKind

    input_files: dict[str, Path] = {}
    parent_id: str | None = None
    mapping = tool_request.mapping_as_dict()

    for input_name, source in mapping.items():
        resolved = None

        # 0. Disambiguation syntax: '<entry_id_prefix>/<filename>'
        #    Lets the LLM target a specific prior tool run's output when
        #    multiple runs produced files with the same name.
        entry_prefix_hint: str | None = None
        filename_hint: str = source
        if "/" in source and not Path(source).exists():
            maybe_prefix, maybe_name = source.split("/", 1)
            # Treat as hint only if the prefix matches a known entry id
            for e in agent.session.entries:
                if e.id.startswith(maybe_prefix):
                    entry_prefix_hint = maybe_prefix
                    filename_hint = maybe_name
                    break

        # 1. Literal path (expand ~ so ~/... paths work)
        p = Path(source).expanduser()
        if p.exists():
            resolved = p

        # --- Phase 1: exact-name matches across all sources ---

        # 2. Search prior session outputs by EXACT name (most recent first).
        #    When an entry_id prefix hint is given, only match that entry.
        if resolved is None:
            for entry in reversed(agent.session.entries):
                if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                    continue
                if entry_prefix_hint and not entry.id.startswith(entry_prefix_hint):
                    continue
                for f in entry.files:
                    if f.role == "output":
                        fp = Path(f.path)
                        if fp.name == filename_hint:
                            resolved = fp
                            parent_id = entry.id
                            break
                if resolved:
                    break

        # 3. Search dataset files by EXACT name.
        if resolved is None and agent.dataset:
            isa_dir = agent.dataset.isa_tab_dir
            for f in agent.dataset.files:
                loc = f.downloaded_path or f.location
                lp = Path(loc)
                # ISA-Tab stores filenames as relative paths — anchor to isa_tab_dir
                if not lp.is_absolute() and isa_dir:
                    lp = isa_dir / lp
                if lp.name == filename_hint and lp.exists():
                    resolved = lp
                    break

        # 4. Search ISA-Tab directory for structural files
        #    (s_study.txt, a_assay.txt, i_investigation.txt, etc.)
        if resolved is None and agent.dataset and agent.dataset.isa_tab_dir:
            candidate = agent.dataset.isa_tab_dir / source
            if candidate.exists():
                resolved = candidate

        # 5. Search ~/.bioledger/datasets/ recursively by EXACT filename.
        #    Covers reference genomes and other studies downloaded via
        #    'bioledger study load' that aren't in the currently-loaded dataset.
        if resolved is None:
            datasets_dir = agent.config.home_dir / "datasets"
            if datasets_dir.is_dir():
                matches = sorted(datasets_dir.rglob(filename_hint))
                if matches:
                    resolved = matches[0]

        # --- Phase 2: fuzzy fallback (only if no exact match found anywhere) ---

        # 6. Search prior session outputs by substring (most recent first).
        if resolved is None:
            for entry in reversed(agent.session.entries):
                if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                    continue
                if entry_prefix_hint and not entry.id.startswith(entry_prefix_hint):
                    continue
                for f in entry.files:
                    if f.role == "output":
                        fp = Path(f.path)
                        if filename_hint in str(fp):
                            resolved = fp
                            parent_id = entry.id
                            break
                if resolved:
                    break

        # 7. Format-based fallback: source didn't match by name, but if the tool
        #    spec declares a format for this input, match the most recent prior
        #    output whose extension matches that format.
        if resolved is None:
            try:
                spec = agent.tool_store.load(tool_request.tool_name)
                declared_format = (
                    spec.execution.inputs.get(input_name, None)
                    if spec.execution.inputs
                    else None
                )
                fmt = getattr(declared_format, "format", None)
            except Exception:
                fmt = None
            if fmt and fmt not in ("any", ""):
                for entry in reversed(agent.session.entries):
                    if entry.kind not in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN):
                        continue
                    if entry_prefix_hint and not entry.id.startswith(entry_prefix_hint):
                        continue
                    for f in entry.files:
                        if f.role == "output":
                            fp = Path(f.path)
                            if fp.suffix.lstrip(".").lower() == fmt.lower():
                                resolved = fp
                                parent_id = entry.id
                                break
                    if resolved:
                        break

        # 8. Search dataset files by substring or declared format.
        if resolved is None and agent.dataset:
            isa_dir = agent.dataset.isa_tab_dir
            for f in agent.dataset.files:
                loc = f.downloaded_path or f.location
                lp = Path(loc)
                if not lp.is_absolute() and isa_dir:
                    lp = isa_dir / lp
                name_match = filename_hint in str(lp)
                if name_match or f.format == filename_hint:
                    if lp.exists():
                        resolved = lp
                        break

        if resolved is None:
            raise ValueError(
                f"Cannot resolve input '{input_name}' from source '{source}'. "
                f"Provide an explicit file path, a filename from a prior tool "
                f"output, or a filename/format from the loaded dataset."
            )
        input_files[input_name] = resolved

    return input_files, parent_id


# --- Crystallize ---


@app.command()
def crystallize(
    session_id: str,
    entry_ids: list[str] = typer.Option(
        None, "--entry", "-e", help="Specific entry IDs to include (default: all)"
    ),
    include_running: bool = typer.Option(
        False, "--include-running", help="Include jobs still running"
    ),
    stage: Path | None = typer.Option(
        None, "--stage", help="Write the crystallized workflow + companion files to this directory"
    ),
) -> None:
    """Convert a session (or selected entries) into a reproducible Nextflow workflow."""
    from bioledger.ledger.models import EntryKind

    store = LedgerStore()
    session = store.load_session(session_id)

    if entry_ids:
        from bioledger.forges.analysisforge.crystallize import (
            to_nextflow_from_entries,
        )

        entries = [e for e in session.entries if e.id in set(entry_ids)]
        workflow_text = to_nextflow_from_entries(
            entries, include_running=include_running
        )
    else:
        from bioledger.forges.analysisforge.crystallize import to_nextflow

        workflow_text = to_nextflow(session, include_running=include_running)

    if stage:
        import shutil

        stage = Path(stage)
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "main.nf").write_text(workflow_text)

        # Stage companion files for entries in the workflow
        tool_entries = [
            e for e in session.entries
            if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
            and e.run_status in ("completed", "failed")
            and (not entry_ids or e.id in set(entry_ids))
            and e.companion_files
        ]
        for entry in tool_entries:
            entry_dir = stage / entry.id
            entry_dir.mkdir(parents=True, exist_ok=True)
            for dest_rel, src_path in entry.companion_files.items():
                src = Path(src_path)
                if src.exists():
                    dest = entry_dir / dest_rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

        if tool_entries:
            console.print(
                f"[green]Crystallized workflow written to {stage / 'main.nf'}[/green]\n"
                f"[green]Staged companion files for {len(tool_entries)} entries[/green]"
            )
        else:
            console.print(
                f"[green]Crystallized workflow written to {stage / 'main.nf'}[/green]"
            )
    else:
        # Text-only mode — warn if any entries have companion_files
        tool_entries = [
            e for e in session.entries
            if e.kind in (EntryKind.TOOL_RUN, EntryKind.SCRIPT_RUN)
            and e.run_status in ("completed", "failed")
            and (not entry_ids or e.id in set(entry_ids))
            and e.companion_files
        ]
        if tool_entries:
            console.print(
                "[yellow]Warning: {} entries have companion files that are not "
                "included in the text output. Use --stage <dir> to write them.[/yellow]"
                .format(len(tool_entries))
            )
        console.print(workflow_text)


# --- Package ---


@app.command()
def package(
    session_id: str,
    entry_ids: list[str] = typer.Option(
        None, "--entry", "-e", help="Specific entry IDs (default: all)"
    ),
    output_dir: Path = typer.Option(
        None, help="Output directory (default: ~/.bioledger/crates/<session_id>)"
    ),
    include_running: bool = typer.Option(
        False, "--include-running", help="Include jobs still running"
    ),
) -> None:
    """Package a session (or selected entries) into an RO-Crate."""
    from bioledger.config import BioLedgerConfig
    from bioledger.forges.analysisforge.executor import get_session_dir
    from bioledger.forges.crateforge.builder import build_rocrate

    config = BioLedgerConfig()
    store = LedgerStore()
    session = store.load_session(session_id)

    if output_dir is None:
        output_dir = config.home_dir / "crates" / session_id

    session_dir = get_session_dir(config.home_dir, session_id)
    crate_dir = build_rocrate(
        session, output_dir, entry_ids=entry_ids,
        include_running=include_running,
        session_dir=session_dir,
    )
    console.print(f"[green]RO-Crate written to {crate_dir}[/green]")
    console.print(
        f"  Entries: {len(entry_ids) if entry_ids else len(session.entries)}"
    )
    console.print("  Includes: workflow.nf, data files, ledger.json")


# --- Library commands ---


def _libraries():
    """Return configured BioLedgerConfig, ToolLibrary, and StudyLibrary."""
    from bioledger.config import BioLedgerConfig
    from bioledger.core.library import StudyLibrary, ToolLibrary

    config = BioLedgerConfig()
    config.ensure_dirs()
    cache_dir = config.home_dir / "cache"
    return config, ToolLibrary(cache_dir), StudyLibrary(cache_dir)


@library_app.command("list")
def library_list() -> None:
    """List all available tools in the remote library."""
    _config, lib, _study_lib = _libraries()
    entries = lib.list_all()

    if not entries:
        console.print("[yellow]No tools found (check network or run 'library refresh')[/yellow]")
        return

    table = Table(title=f"Tool Library ({len(entries)} tools)")
    table.add_column("Name", style="cyan")
    table.add_column("Family")
    table.add_column("Version")
    table.add_column("Description")
    table.add_column("Categories")

    for e in entries:
        table.add_row(
            e["name"],
            e.get("family", ""),
            e.get("version", ""),
            e.get("description", "")[:60],
            ", ".join(e.get("categories", [])),
        )
    console.print(table)


@library_app.command("search")
def library_search(
    query: str = typer.Argument(..., help="Search query"),
) -> None:
    """Search the tool library by name, category, or description."""
    _config, lib, _study_lib = _libraries()
    results = lib.search(query)

    if not results:
        console.print(f"[yellow]No tools matching '{query}'[/yellow]")
        return

    table = Table(title=f"Search results for '{query}' ({len(results)} matches)")
    table.add_column("Name", style="cyan")
    table.add_column("Family")
    table.add_column("Description")

    for e in results:
        table.add_row(e["name"], e.get("family", ""), e.get("description", "")[:70])
    console.print(table)


@library_app.command("show")
def library_show(
    name: str = typer.Argument(..., help="Tool name to show details for"),
) -> None:
    """Show detailed info about a library tool."""
    _config, lib, _study_lib = _libraries()
    entry = lib.get(name)

    if not entry:
        console.print(f"[red]Tool '{name}' not found in library[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{entry['name']}[/bold cyan] v{entry.get('version', '?')}")
    console.print(f"  Family: {entry.get('family', '-')}")
    console.print(f"  Description: {entry.get('description', '-')}")
    console.print(f"  Container: {entry.get('container', '-')}")
    console.print(f"  Categories: {', '.join(entry.get('categories', []))}")
    console.print(f"  Inputs: {', '.join(entry.get('inputs', []))}")
    console.print(f"  Outputs: {', '.join(entry.get('outputs', []))}")
    console.print(f"  Path: {entry.get('path', '-')}")


@library_app.command("import")
def library_import(
    name: str = typer.Argument(..., help="Tool name to import"),
    ref: str = typer.Option("main", help="Git ref (branch/tag/commit) to pin"),
) -> None:
    """Import a tool from the library into the local store."""
    from bioledger.toolspec.store import ToolStore

    config, lib, _study_lib = _libraries()
    store = ToolStore(tools_dir=config.home_dir / "tools")

    # Find the tool's path in the library index
    entry = lib.get(name)
    if not entry:
        console.print(f"[red]Tool '{name}' not found in library[/red]")
        raise typer.Exit(1)

    path = entry["path"]
    try:
        spec = store.import_from_library(path=path, ref=ref)
        console.print(f"[green]Imported '{spec.name}' (ref={ref})[/green]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@library_app.command("refresh")
def library_refresh() -> None:
    """Force-refresh the tool library index from remote."""
    _config, lib, _study_lib = _libraries()
    lib.refresh()
    entries = lib.list_all()
    console.print(f"[green]Refreshed: {len(entries)} tools available[/green]")


# --- Study commands ---


@study_app.command("list")
def study_list() -> None:
    """List all available studies in the remote library."""
    _config, _tool_lib, lib = _libraries()
    entries = lib.list_all()

    if not entries:
        console.print("[yellow]No studies found (check network or run 'study refresh')[/yellow]")
        return

    table = Table(title=f"Study Library ({len(entries)} studies)")
    table.add_column("Accession", style="cyan")
    table.add_column("Type")
    table.add_column("Organism")
    table.add_column("Title")
    table.add_column("Files")

    for e in entries:
        table.add_row(
            e["accession"],
            e.get("study_type", "").replace("_", " "),
            e.get("organism", ""),
            (e.get("title", "") or e.get("description", ""))[:50],
            str(e.get("file_count", 0)),
        )
    console.print(table)


@study_app.command("search")
def study_search(
    query: str = typer.Argument(..., help="Search query"),
) -> None:
    """Search the study library by organism, accession, or description."""
    _config, _tool_lib, lib = _libraries()
    results = lib.search(query)

    if not results:
        console.print(f"[yellow]No studies matching '{query}'[/yellow]")
        return

    table = Table(title=f"Search results for '{query}' ({len(results)} matches)")
    table.add_column("Accession", style="cyan")
    table.add_column("Organism")
    table.add_column("Title")

    for e in results:
        table.add_row(
            e["accession"],
            e.get("organism", ""),
            (e.get("title", "") or "")[:60],
        )
    console.print(table)


@study_app.command("show")
def study_show(
    accession: str = typer.Argument(..., help="Study accession to show"),
) -> None:
    """Show detailed info about a library study."""
    _config, _tool_lib, lib = _libraries()
    entry = lib.get(accession)

    if not entry:
        console.print(f"[red]Study '{accession}' not found in library[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{entry['accession']}[/bold cyan]")
    console.print(f"  Type: {entry.get('study_type', '-').replace('_', ' ')}")
    console.print(f"  Organism: {entry.get('organism', '-')}")
    console.print(f"  Title: {entry.get('title', '-')}")
    console.print(f"  Description: {entry.get('description', '-')}")
    console.print(f"  Formats: {', '.join(entry.get('formats', []))}")
    console.print(f"  Files: {entry.get('file_count', 0)}")


@study_app.command("load")
def study_load(
    accession: str = typer.Argument(..., help="Study accession to load"),
    download: bool = typer.Option(True, help="Download remote files"),
) -> None:
    """Load a study from the library (downloads files via manifest)."""
    import asyncio

    config, _tool_lib, lib = _libraries()
    datasets_dir = config.home_dir / "datasets" / accession

    async def _download() -> None:
        await lib.download(accession, datasets_dir, with_data=download)

    try:
        asyncio.run(_download())
        if download:
            console.print(f"[green]Study '{accession}' downloaded to {datasets_dir}[/green]")
        else:
            console.print(
                f"[green]Study metadata for '{accession}' saved to {datasets_dir}[/green]"
            )
            console.print("  Run with --download to fetch data files")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@study_app.command("refresh")
def study_refresh() -> None:
    """Force-refresh the study library index from remote."""
    _config, _tool_lib, lib = _libraries()
    lib.refresh()
    entries = lib.list_all()
    console.print(f"[green]Refreshed: {len(entries)} studies available[/green]")


# --- Config ---


@config_app.command("validate")
def config_validate() -> None:
    """Validate engine configs for common mistakes and mismatches."""
    from bioledger.config import BioLedgerConfig
    from bioledger.core.engines.validate import (
        format_warnings,
        validate_engine_config,
    )

    config = BioLedgerConfig()
    result = validate_engine_config(config.engines)
    console.print(format_warnings(result))
    if not result.ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
