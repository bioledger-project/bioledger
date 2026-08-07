from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from bioledger.config import BioLedgerConfig
from bioledger.ledger.models import LedgerSession
from bioledger.ledger.store import LedgerStore


@dataclass
class ForgeDeps:
    """Shared dependencies injected into every forge agent."""

    session: LedgerSession
    config: BioLedgerConfig
    store: LedgerStore | None = None
    context_mode: Literal["chat", "utility"] = "utility"

    def message_history(self, max_messages: int | None = None) -> list[ModelMessage]:
        """Build pydantic-ai message history from session chat messages.
        Only returns messages when context_mode == 'chat'.
        max_messages limits how many recent messages to include (None = all)."""
        if self.context_mode != "chat":
            return []
        msgs = self.session.chat_messages
        if max_messages and len(msgs) > max_messages:
            msgs = msgs[-max_messages:]
        history: list[ModelMessage] = []
        for m in msgs:
            if m.role == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
            elif m.role == "assistant":
                history.append(ModelResponse(parts=[TextPart(content=m.content)]))
            # system messages are handled via agent instructions, not history
        return history

    def session_summary(self) -> str:
        """One-paragraph summary of what happened in this session so far.
        Useful as a system-prompt prefix for conversational agents."""
        _MAX_OUTPUTS = 10
        entries = self.session.entries
        if not entries:
            return "This is a new session with no prior actions."
        tool_runs = [e for e in entries if e.kind.value in ("tool_run", "script_run")]
        last_outputs = []
        for e in tool_runs[-3:]:  # last 3 tool runs
            name = e.tool_spec_name or "unknown"
            out_files = [f.path for f in e.files if f.role == "output"]
            if len(out_files) > _MAX_OUTPUTS:
                shown = ", ".join(out_files[:_MAX_OUTPUTS])
                last_outputs.append(
                    f"{name} → {shown} ... ({len(out_files)} total)"
                )
            else:
                last_outputs.append(
                    f"{name} → {', '.join(out_files) or 'no output files'}"
                )
        summary = (
            f"Session '{self.session.name or self.session.id}' has "
            f"{len(entries)} entries ({len(tool_runs)} tool/script runs). "
        )
        if last_outputs:
            summary += "Recent: " + "; ".join(last_outputs)
        return summary


def make_agent(
    config: BioLedgerConfig,
    instructions: str = "",
    tools: list | None = None,
    output_type: type | None = None,
    model: str | None = None,
    task: str | None = None,
) -> Agent:
    """Create a pydantic-ai agent pre-wired with BioLedger deps.
    If task is provided, uses config.llm.model_for_task(task) for model selection.
    Explicit model param overrides task-based selection."""
    resolved_model = model or (
        config.llm.model_for_task(task) if task else config.llm.default_model
    )
    return Agent(
        resolved_model,
        deps_type=ForgeDeps,
        instructions=instructions,
        tools=tools or [],
        output_type=output_type or str,
    )
