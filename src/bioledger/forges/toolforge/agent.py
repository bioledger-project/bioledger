from __future__ import annotations

from typing import Any

from bioledger.config import BioLedgerConfig
from bioledger.core.llm.agents import ForgeDeps, make_agent
from bioledger.toolspec.models import ExecutionSpec, ExecutionSpecDraft
from bioledger.toolspec.validate import ValidationIssue


class ToolForgeAgent:
    """Holds lazy-created agents for all ToolForge LLM tasks.
    Agents are created on first use to avoid initialization errors
    when API keys aren't configured for unused providers."""

    def __init__(self, config: BioLedgerConfig):
        self._config = config
        self._agents: dict[str, Any] = {}

    def _get_agent(self, task: str, instructions: str, output_type: type) -> Any:
        """Get or create an agent for the given task."""
        if task not in self._agents:
            self._agents[task] = make_agent(
                self._config,
                task=task,
                instructions=instructions,
                output_type=output_type,
            )
        return self._agents[task]

    @property
    def _parse_agent(self):
        return self._get_agent(
            "parse_fallback",
            instructions=(
                "You are a bioinformatics tool spec parser. "
                "Extract name, version, container, command, inputs, outputs, parameters "
                "from the provided tool definition. Return a valid ExecutionSpec."
            ),
            output_type=ExecutionSpecDraft,
        )

    @property
    def _fix_agent(self):
        return self._get_agent(
            "fix_issues",
            instructions=(
                "You are a BioLedger tool spec validator. "
                "Fix the listed issues in the ExecutionSpec. Return a corrected ExecutionSpec."
            ),
            output_type=ExecutionSpecDraft,
        )

    @property
    def _review_agent(self):
        return self._get_agent(
            "review",
            instructions=(
                "You are a bioinformatics tool expert. Review this ExecutionSpec for "
                "conceptual correctness: does the command make sense? Are formats correct? "
                "Are parameter defaults reasonable? Return a list of issues (strings). "
                "Return an empty list if everything looks good."
            ),
            output_type=list[str],
        )

    async def parse(
        self, source_text: str, source_type: str, error: str, deps: ForgeDeps
    ) -> ExecutionSpec:
        """LLM fallback: parse a tool definition when programmatic parser fails."""
        prompt = (
            f"The programmatic {source_type} parser failed with: {error}\n\n"
            f"Parse this {source_type} and produce a valid ExecutionSpec:\n\n{source_text}"
        )
        result = await self._parse_agent.run(prompt, deps=deps)
        return result.output.to_execution_spec()

    async def fix(
        self, spec: ExecutionSpec, issues: list[ValidationIssue], deps: ForgeDeps
    ) -> ExecutionSpec:
        """LLM fixes validation issues in a spec."""
        issues_str = "\n".join(f"- {i.field}: {i.message}" for i in issues)
        draft = ExecutionSpecDraft.from_execution_spec(spec)
        prompt = f"Fix these issues:\n{issues_str}\n\nSpec:\n{draft.model_dump_json(indent=2)}"
        result = await self._fix_agent.run(prompt, deps=deps)
        return result.output.to_execution_spec()

    async def review(
        self, spec: ExecutionSpec, source: str, deps: ForgeDeps
    ) -> list[str]:
        """LLM reviews the spec for conceptual correctness."""
        prompt = (
            f"Review this spec (imported from {source}):\n\n"
            f"{spec.model_dump_json(indent=2)}"
        )
        result = await self._review_agent.run(prompt, deps=deps)
        return result.output
