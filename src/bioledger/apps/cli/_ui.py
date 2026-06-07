"""Shared UI helpers for CLI commands."""

from __future__ import annotations

from rich.console import Console

from bioledger.toolspec.validate import ValidationResult

_SEVERITY_COLORS = {"error": "red", "warning": "yellow", "info": "dim"}


def print_validation_issues(
    result: ValidationResult,
    console: Console,
    *,
    show_field: bool = False,
    indent: str = "  ",
) -> None:
    """Render a ValidationResult's issues to the console with severity colors.

    Args:
        result: Validation result to print.
        console: Rich console to write to.
        show_field: When True, prefix messages with the offending field path.
        indent: Leading indent applied to every line.
    """
    for issue in result.issues:
        color = _SEVERITY_COLORS.get(issue.severity.value, "dim")
        prefix = f"{issue.field}: " if show_field else ""
        console.print(
            f"{indent}[{color}]{issue.severity.value}[/{color}] {prefix}{issue.message}"
        )
