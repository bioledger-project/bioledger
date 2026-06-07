from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from bioledger.config import BioLedgerConfig
from bioledger.core.llm.agents import ForgeDeps, make_agent
from bioledger.toolspec.models import ExecutionSpec, ExecutionSpecDraft, ToolSpec
from bioledger.toolspec.validate import ValidationIssue


class GalaxyFixResult(BaseModel):
    """Structured output of the Galaxy-import fix step.

    The LLM returns a full corrected spec (so it can add missing parameters,
    not just rewrite the command), plus human-readable change/issue notes.
    """

    fixed_spec: ExecutionSpecDraft
    changes_made: list[str] = Field(default_factory=list)
    remaining_issues: list[str] = Field(default_factory=list)


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

    @property
    def _enrich_agent(self):
        return self._get_agent(
            "enrich_export",
            instructions=(
                "You are an expert in Galaxy tool XML and Nextflow DSL2. "
                "Review and optionally improve the generated output. "
                "Add help text, citations, or fix structural issues. "
                "Return the improved output as a string."
            ),
            output_type=str,
        )

    @property
    def _galaxy_fix_agent(self):
        return self._get_agent(
            "galaxy_import_fix",
            instructions=(
                "You are a Galaxy-to-BioLedger import expert. Your job is to ensure the "
                "imported tool spec is runnable. Fix any issues that would stand in the way "
                "of this tool being runnable, and note anything you cannot fix.\n\n"
                "Use the validation issues, parser warnings, and Galaxy residue as a "
                "starting point — but also look beyond them. Fix anything else you spot "
                "that looks broken: missing parameters, incorrect command syntax, unresolved "
                "macros, incomplete inputs/outputs, etc.\n\n"
                "Common command-template fixes:\n"
                "  re.sub('[^\\w\\-\\s]', '_', str($var.element_identifier))\n"
                "    → {{ inputs.var | basename | replace(' ', '_') }}\n"
                "  $var.element_identifier  → {{ inputs.var | basename }}\n"
                "  $var.ext                 → {{ inputs.var | splitext | last }}\n"
                "  $var.files_path          → {{ outputs._dir }}\n"
                "  {{inputs.var}}.dataset   → {% if inputs.var %} (in conditional)\n"
                "  str($var)                → just $var (Jinja2 auto-coerces)\n"
                "  {{...}}.endswith('.gz')  → {% if (inputs.var | splitext | last) "
                "== '.gz' %}\n"
                "  ${GALAXY_SLOTS:-N}       → add 'threads' parameter (default N), "
                "use {{ parameters.threads }}\n"
                "  ${_GALAXY_JOB_TMP_DIR}   → /tmp, or add a 'temp_dir' parameter\n"
                "  #import statements       → remove (Python imports are inert in Jinja2)\n\n"
                "IMPORTANT rules:\n"
                "  - NEVER change the container image. The programmatic pipeline already "
                "resolved the correct biocontainers image. If you think it's wrong, "
                "note it in remaining_issues instead.\n"
                "  - NEVER replace output formats with 'any'. If a format is non-standard "
                "(e.g. 'hyphy_results.json', 'markdown'), keep it as-is. A non-standard "
                "format is more useful than 'any' because it enables downstream tooling.\n"
                "  - Preserve existing output format values unless you have concrete "
                "evidence from the original XML that they are wrong.\n\n"
                "Return a GalaxyFixResult containing:\n"
                "  - fixed_spec: the FULL corrected ExecutionSpec (preserve existing\n"
                "    name, version, container, inputs, outputs; only modify what needs\n"
                "    to change; ADD any parameters required by the new command).\n"
                "  - changes_made: short bullet descriptions of every edit you made.\n"
                "  - remaining_issues: anything that still needs manual review."
            ),
            output_type=GalaxyFixResult,
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

    async def enrich_export(
        self,
        spec: ToolSpec,
        generated_output: str,
        target_format: str,
        deps: ForgeDeps,
    ) -> str:
        """LLM reviews and improves generated Galaxy XML or Nextflow DSL2."""
        prompt = (
            f"Original BioLedger spec:\n{spec.model_dump_json(indent=2)}\n\n"
            f"Generated {target_format}:\n{generated_output}"
        )
        result = await self._enrich_agent.run(prompt, deps=deps)
        return result.output

    async def fix_galaxy_import(
        self,
        spec: ExecutionSpec,
        original_xml: str,
        deps: ForgeDeps,
        *,
        parser_warnings: list[str] | None = None,
        residue: list[str] | None = None,
        validation_issues: list[ValidationIssue] | None = None,
    ) -> tuple[ExecutionSpec, list[str], list[str]]:
        """Repair Galaxy-specific constructs left over after programmatic conversion.

        Single step, broad scope: fix anything that would stand in the way of
        the tool being runnable. Uses validation issues as a starting point,
        but also addresses parser warnings, Galaxy/Cheetah residue, and any
        other issues the LLM spots.

        Returns ``(fixed_spec, changes_made, remaining_issues)``.
        """
        draft = ExecutionSpecDraft.from_execution_spec(spec)

        context_lines: list[str] = []
        if validation_issues:
            context_lines.append("Validation issues:")
            context_lines.extend(f"  - {i.field}: {i.message}" for i in validation_issues)
        if parser_warnings:
            context_lines.append("Programmatic parser warnings:")
            context_lines.extend(f"  - {w}" for w in parser_warnings)
        if residue:
            context_lines.append("Cheetah/Galaxy residue detected in command:")
            context_lines.extend(f"  - {r}" for r in residue)

        context_block = "\n".join(context_lines) + "\n\n" if context_lines else ""

        prompt = (
            f"{context_block}"
            f"Original Galaxy XML:\n\n{original_xml}\n\n"
            f"Current BioLedger spec (JSON):\n{draft.model_dump_json(indent=2)}\n\n"
            "Fix the spec per your instructions and return a GalaxyFixResult."
        )
        result = await self._galaxy_fix_agent.run(prompt, deps=deps)
        fix: GalaxyFixResult = result.output
        return (
            fix.fixed_spec.to_execution_spec(),
            list(fix.changes_made),
            list(fix.remaining_issues),
        )
