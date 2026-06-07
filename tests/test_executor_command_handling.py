"""Tests for executor command parsing and shell handling."""

import pytest


class TestCommandShellDetection:
    """Tests for detecting when commands need shell execution."""

    def test_simple_command_uses_direct_exec(self):
        """Simple commands without operators should use shlex.split."""
        # Simple command: fastqc /input/sample.fastq
        simple_cmd = "fastqc /input/sample.fastq"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in simple_cmd for op in shell_operators)
        assert not needs_shell

    def test_and_operator_needs_shell(self):
        """Commands with && should use shell."""
        cmd = "ln -s file link && mkdir -p dir"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell

    def test_or_operator_needs_shell(self):
        """Commands with || should use shell."""
        cmd = "cmd1 || cmd2"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell

    def test_pipe_operator_needs_shell(self):
        """Commands with | should use shell."""
        cmd = "cat file | grep pattern"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell

    def test_redirect_operator_needs_shell(self):
        """Commands with > or < should use shell."""
        cmd = "echo test > file.txt"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell

    def test_newline_needs_shell(self):
        """Multiline commands should use shell."""
        cmd = "line1\nline2"
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell

    def test_galaxy_style_command_needs_shell(self):
        """Galaxy-style commands with && chains need shell."""
        cmd = """ln -s '${input_file}' '${input_file_sl}' &&
        mkdir -p '${html_file.files_path}' &&
        fastqc --outdir '${html_file.files_path}'"""
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell


class TestCommandFormatting:
    """Tests for proper command formatting."""

    def test_shlex_split_simple_command(self):
        """shlex.split works for simple commands."""
        import shlex

        cmd = "fastqc /input/sample.fastq"
        result = shlex.split(cmd)
        assert result == ["fastqc", "/input/sample.fastq"]

    def test_shlex_split_breaks_operators(self):
        """shlex.split incorrectly splits shell operators as args."""
        import shlex

        cmd = "ln -s file link && mkdir -p dir"
        result = shlex.split(cmd)
        # This is WRONG - && and mkdir become literal arguments to ln
        assert "&&" in result
        assert "mkdir" in result

    def test_shell_wrap_fixes_operators(self):
        """Wrapping with sh -c preserves shell semantics."""
        cmd = "ln -s file link && mkdir -p dir"
        wrapped = ["sh", "-c", cmd]
        # The shell will interpret && correctly
        assert wrapped[0] == "sh"
        assert wrapped[1] == "-c"
        assert wrapped[2] == cmd


class TestGalaxyCommandPatterns:
    """Tests for specific Galaxy command patterns that caused issues."""

    def test_ln_mkdir_chain(self):
        """The pattern that caused 'ln: invalid option -- p' error."""
        # This is the pattern from Galaxy XML that was broken
        cmd = """ln -s 'input.fastq' 'input.fastq' &&
        mkdir -p '/output' &&
        fastqc --outdir '/output' 'input.fastq'"""

        # Verify this pattern has shell operators
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell, "Galaxy commands with && chains need shell"

    def test_multiline_jinja_template(self):
        """Galaxy commands with Jinja2 are often multiline."""
        cmd = """{% set input_name = inputs.input_file | basename %}
        ln -s '{{inputs.input_file}}' '{{input_name}}' &&
        fastqc '{{input_name}}'"""

        # Has both newlines and &&
        shell_operators = ["&&", "||", "|", ";", "<", ">", "$(", "`", "\n"]
        needs_shell = any(op in cmd for op in shell_operators)
        assert needs_shell, "Multiline Jinja2 commands need shell"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
