import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bioledger.forges.toolforge.translators.galaxy_context import (
    GalaxyImportContext,
    GalaxySuiteContext,
    resolve_import_context,
)
from bioledger.forges.toolforge.translators.galaxy_macros import (
    expand_text_macros,
    expand_xml_macros,
    load_macros,
)


# ── galaxy_context tests ──────────────────────────────────────────────

class TestResolveImportContext:
    def test_resolve_context_file(self, tmp_path: Path):
        xml_file = tmp_path / "tool.xml"
        xml_file.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="test" name="test" version="1.0">'
            '<description>Test</description>'
            '<command>echo hello</command>'
            '</tool>'
        )
        ctx = resolve_import_context(xml_file)
        assert ctx.xml_path == xml_file
        assert ctx.macros_xml_path is None
        assert ctx.assets == []
        assert ctx.base_dir == tmp_path

    def test_resolve_context_dir_with_macros(self, tmp_path: Path):
        # Create tool XML
        xml_file = tmp_path / "mytool.xml"
        xml_file.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="mytool" name="mytool" version="@TOOL_VERSION@">'
            '<description>My tool</description>'
            '<requirements>'
            '<expand macro="requirements"/>'
            '</requirements>'
            '<command>python script.py</command>'
            '</tool>'
        )
        # Create macros.xml
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text(
            '<?xml version="1.0"?>\n'
            '<macros>'
            '<macro name="requirements">'
            '<requirement type="package">samtools</requirement>'
            '</macro>'
            '</macros>'
        )
        # Create auxiliary script
        script = tmp_path / "script.py"
        script.write_text("print('hello')")

        ctx = resolve_import_context(tmp_path)
        assert ctx.xml_path == xml_file
        assert ctx.macros_xml_path == macros_file
        assert len(ctx.assets) == 1
        assert ctx.assets[0].name == "script.py"
        assert ctx.base_dir == tmp_path

    def test_resolve_context_dir_no_macros(self, tmp_path: Path):
        xml_file = tmp_path / "tool.xml"
        xml_file.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool" name="tool" version="1.0">'
            '<description>Standalone</description>'
            '<command>echo hello</command>'
            '</tool>'
        )
        ctx = resolve_import_context(tmp_path)
        assert ctx.xml_path == xml_file
        assert ctx.macros_xml_path is None
        assert ctx.assets == []

    def test_resolve_context_dir_finds_script(self, tmp_path: Path):
        xml_file = tmp_path / "tool.xml"
        xml_file.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool" name="tool" version="1.0">'
            '<description>Test</description>'
            '<command>python __tool_directory__/helper.py --in $input</command>'
            '</tool>'
        )
        script = tmp_path / "helper.py"
        script.write_text("import sys; print(sys.argv)")

        ctx = resolve_import_context(tmp_path)
        assert len(ctx.assets) == 1
        assert ctx.assets[0].name == "helper.py"

    def test_resolve_context_nonexistent_path(self):
        with pytest.raises(FileNotFoundError):
            resolve_import_context(Path("/nonexistent/path"))

    def test_resolve_context_dir_no_tool_xml(self, tmp_path: Path):
        # Only macros.xml, no tool XML
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text("<macros></macros>")
        with pytest.raises(ValueError, match="No Galaxy tool XML found"):
            resolve_import_context(tmp_path)

    def test_resolve_context_suite(self, tmp_path: Path):
        # Two tool XMLs sharing one macros.xml
        tool_a = tmp_path / "tool_a.xml"
        tool_a.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool_a" name="tool_a" version="1.0">'
            '<description>A</description>'
            '<command>echo a</command>'
            '</tool>'
        )
        tool_b = tmp_path / "tool_b.xml"
        tool_b.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool_b" name="tool_b" version="1.0">'
            '<description>B</description>'
            '<command>echo b</command>'
            '</tool>'
        )
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text("<macros></macros>")

        ctx = resolve_import_context(tmp_path)
        assert isinstance(ctx, GalaxySuiteContext)
        assert len(ctx.xml_paths) == 2
        assert ctx.xml_paths[0] == tool_a
        assert ctx.xml_paths[1] == tool_b
        assert ctx.macros_xml_path == macros_file
        assert ctx.base_dir == tmp_path


# ── galaxy_macros tests ───────────────────────────────────────────────

class TestLoadMacros:
    def test_load_macros(self, tmp_path: Path):
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text(
            '<?xml version="1.0"?>\n'
            '<macros>'
            '<token name="@TOOL_VERSION@">2.5.38</token>'
            '<token name="@VERSION_SUFFIX@">0</token>'
            '<macro name="requirements">'
            '<requirement type="package">samtools</requirement>'
            '</macro>'
            '<macro name="inputs">'
            '<param name="input" type="data" format="bam"/>'
            '</macro>'
            '</macros>'
        )
        result = load_macros(macros_file)
        assert "requirements" in result.xml_macros
        assert "inputs" in result.xml_macros
        assert "TOOL_VERSION" in result.tokens
        assert result.tokens["TOOL_VERSION"] == "2.5.38"
        assert result.tokens["VERSION_SUFFIX"] == "0"

    def test_load_macros_empty(self, tmp_path: Path):
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text('<?xml version="1.0"?>\n<macros></macros>')
        result = load_macros(macros_file)
        assert result.xml_macros == {}
        assert result.tokens == {}


class TestExpandXmlMacros:
    def test_expand_single_macro(self):
        xml = (
            '<tool id="test" name="test" version="1.0">'
            '<requirements>'
            '<expand macro="requirements"/>'
            '</requirements>'
            '<command>echo hello</command>'
            '</tool>'
        )
        macro_el = ET.fromstring(
            '<requirement type="package">samtools</requirement>'
        )
        result = expand_xml_macros(xml, {"requirements": macro_el})
        assert "<expand" not in result
        assert "<requirement" in result
        assert "samtools" in result

    def test_expand_multiple_macros(self):
        xml = (
            '<tool id="test" name="test" version="1.0">'
            '<requirements>'
            '<expand macro="reqs"/>'
            '</requirements>'
            '<inputs>'
            '<expand macro="inputs"/>'
            '</inputs>'
            '<command>echo hello</command>'
            '</tool>'
        )
        reqs = ET.fromstring(
            '<requirement type="package">samtools</requirement>'
        )
        inputs = ET.fromstring(
            '<param name="input" type="data" format="bam"/>'
        )
        result = expand_xml_macros(xml, {"reqs": reqs, "inputs": inputs})
        assert result.count("<expand") == 0
        assert "<requirement" in result
        assert "samtools" in result
        assert "input" in result

    def test_no_macros_to_expand(self):
        xml = (
            '<tool id="test" name="test" version="1.0">'
            '<command>echo hello</command>'
            '</tool>'
        )
        result = expand_xml_macros(xml, {})
        assert result == xml

    def test_unknown_macro_name(self):
        xml = (
            '<tool id="test" name="test" version="1.0">'
            '<requirements>'
            '<expand macro="unknown"/>'
            '</requirements>'
            '<command>echo hello</command>'
            '</tool>'
        )
        result = expand_xml_macros(xml, {"other": ET.fromstring("<x/>")})
        # Unknown macro should be left as-is (not expanded)
        assert "<expand" in result


class TestExpandTextMacros:
    def test_resolve_known_macros(self):
        xml = '<tool version="@TOOL_VERSION@" id="@TOOL_ID@">'
        known = {"TOOL_VERSION": "1.0.0", "TOOL_ID": "mytool"}
        result, unresolved = expand_text_macros(xml, known)
        assert result == '<tool version="1.0.0" id="mytool">'
        assert unresolved == []

    def test_partial_resolution(self):
        xml = '<tool version="@TOOL_VERSION@" id="@TOOL_ID@">'
        known = {"TOOL_VERSION": "1.0.0"}
        result, unresolved = expand_text_macros(xml, known)
        assert "1.0.0" in result
        assert "@TOOL_ID@" in result
        assert "TOOL_ID" in unresolved

    def test_no_macros(self):
        xml = '<tool version="1.0.0" id="mytool">'
        result, unresolved = expand_text_macros(xml, {})
        assert result == xml
        assert unresolved == []

    def test_deduplicates_unresolved(self):
        xml = '@VER@ @VER@ @VER@'
        result, unresolved = expand_text_macros(xml, {})
        assert unresolved == ["VER"]  # Only one entry, not three

    def test_tokens_from_macros_xml(self, tmp_path: Path):
        """Tokens defined in macros.xml should resolve @MACRO@ references."""
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text(
            '<?xml version="1.0"?>\n'
            '<macros>'
            '<token name="@TOOL_VERSION@">2.5.38</token>'
            '<token name="@VERSION_SUFFIX@">0</token>'
            '</macros>'
        )
        macro_set = load_macros(macros_file)
        xml = '<tool version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@">'
        expanded, unresolved = expand_text_macros(xml, macro_set.tokens)
        assert expanded == '<tool version="2.5.38+galaxy0">'
        assert unresolved == []

    def test_inert_galaxy_tokens_stripped(self):
        """Galaxy-specific inert tokens should be silently removed."""
        xml = '@SYMLINK_FILES@ echo hello @SHELL_OPTIONS@ @ERRORS@'
        expanded, unresolved = expand_text_macros(xml, {})
        assert expanded == ' echo hello  '
        assert unresolved == []


class TestSuiteContext:
    """Test that suite imports share macros.xml across all tool XMLs."""

    def test_suite_context_shares_macros(self, tmp_path: Path):
        # Create macros.xml with tokens
        macros_file = tmp_path / "macros.xml"
        macros_file.write_text(
            '<?xml version="1.0"?>\n'
            '<macros>'
            '<token name="@TOOL_VERSION@">2.5.93</token>'
            '<token name="@VERSION_SUFFIX@">2</token>'
            '<xml name="requirements">'
            '<requirement type="package">hyphy</requirement>'
            '</xml>'
            '</macros>'
        )
        # Create two tool XMLs
        tool_a = tmp_path / "tool_a.xml"
        tool_a.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool_a" name="Tool A" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@">'
            '<description>A</description>'
            '<requirements><expand macro="requirements"/></requirements>'
            '<command>tool_a --in $input</command>'
            '<inputs><param name="input" type="data" format="fasta"/></inputs>'
            '<outputs><data name="out" format="fasta"/></outputs>'
            '</tool>'
        )
        tool_b = tmp_path / "tool_b.xml"
        tool_b.write_text(
            '<?xml version="1.0"?>\n'
            '<tool id="tool_b" name="Tool B" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@">'
            '<description>B</description>'
            '<requirements><expand macro="requirements"/></requirements>'
            '<command>tool_b --in $input</command>'
            '<inputs><param name="input" type="data" format="fasta"/></inputs>'
            '<outputs><data name="out" format="fasta"/></outputs>'
            '</tool>'
        )

        ctx = resolve_import_context(tmp_path)
        assert isinstance(ctx, GalaxySuiteContext)
        assert ctx.macros_xml_path == macros_file
        assert len(ctx.xml_paths) == 2

        # Each tool should be able to use the shared macros
        from bioledger.forges.toolforge.translators.galaxy_macros import (
            MacroSet,
            load_macros,
        )
        macro_set = load_macros(macros_file)
        assert macro_set.tokens["TOOL_VERSION"] == "2.5.93"
        assert macro_set.tokens["VERSION_SUFFIX"] == "2"
        assert "requirements" in macro_set.xml_macros
