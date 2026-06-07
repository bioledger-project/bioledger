# Galaxy Importer — Implementation Plan

**Date:** 2026-04-26  
**Based on:** `docs/galaxy-import-refactor-summary.md`  
**Status:** Planning phase

---

## Overview

This plan covers all outstanding tasks from the refactor summary, plus one new initiative: **directory-based Galaxy tool import with macro resolution**. Tasks are organized into phases by priority and dependency order.

---

## Phase 1: Unified File-or-Directory Import with Macro Resolution (NEW)

**Priority:** High  
**Estimated Effort:** 3-4 days  
**Dependencies:** None

### Design Principle

**One command, same flow, regardless of input type.** The user runs:

```
bioledger tool import /path/to/tool.xml
bioledger tool import /path/to/tool/dir
```

BioLedger figures out what it got and does the right thing. No new flags, no mode switching. The import pipeline is the same — it just starts with more context when given a directory.

**Programmatic first, LLM only when needed.** Every step tries to resolve things deterministically. When the LLM does run, it gets a single, broad mandate: fix anything that would stand in the way of this tool being runnable, and note what it couldn't fix.

### Problem

The current importer assumes a single, self-contained XML file. Real Galaxy tools from IUC (e.g., `fastp`, `hyphy_busted`) depend on external `macros.xml` files that define:
- Package requirements (`<macro name="requirements">`)
- Input sections (`<macro name="inputs">`)
- Citation blocks (`<macro name="citations">`)
- Complex parameter groups (`<macro name="adapter_trimming_options">`)
- Conditional branches (`<macro name="branches">`)

Currently, `<import>macros.xml</import>` and `<expand macro="name"/>` elements are silently ignored, resulting in incomplete tool specs.

### Unified Import Flow

```
bioledger tool import <path>
  │
  ├── path is a file?
  │     └── xml_str = read(path)
  │     └── context = {macros: None, assets: [], base_dir: path.parent}
  │
  └── path is a directory?
        └── Scan directory:
              ├── Find tool XML: *.xml that looks like a Galaxy tool (has <tool> root)
              ├── Find macros.xml if present
              ├── Find auxiliary files: scripts referenced in <command>/<configfiles>
              └── context = {macros: parsed_macros, assets: [script_paths], base_dir: path}
  │
  ▼
  Step 1: Resolve macros (programmatic)
  │   ├── If macros.xml found → expand <expand macro="..."/> tags
  │   ├── Resolve @MACRO@ text tokens where values are known
  │   └── Collect unresolved macros as warnings (not errors)
  │
  ▼
  Step 2: Parse expanded XML (programmatic)
  │   └── parse_galaxy_xml(xml_str) → spec + warnings
  │
  ▼
  Step 3: Validate (programmatic)
  │   └── validate_execution(spec) → issues or clean
  │
  ▼
  Step 4: LLM fix (only if --use-llm AND something is wrong)
  │   Single step, broad scope:
  │   - Input: spec + validation_issues + parser_warnings + residue + original_xml
  │   - Prompt: "Fix any issues that would stand in the way of this tool being
  │              runnable. Use the validation issues as a starting point, but also
  │              address parser warnings, Galaxy residue, and anything else you
  │              spot that looks broken. Note anything you can't fix."
  │   - Output: GalaxyFixResult(fixed_spec, changes_made, remaining_issues)
  │
  ▼
  Step 5: Finalize (same for file or dir)
      └── validate → dry-run or save
```

### Implementation Steps

#### 1.1. Create directory context resolver
- **New file:** `src/bioledger/forges/toolforge/translators/galaxy_context.py`
- **Function:** `resolve_import_context(path) -> GalaxyImportContext`
- **Dataclass:** `GalaxyImportContext(xml_path, macros_xml_path, assets, base_dir)`
- **Logic:**
  - If `path.is_file()` and `.xml` → return context with that file, no macros, no assets
  - If `path.is_dir()` → scan for:
    - **Tool XML:** `*.xml` files with `<tool>` root element (pick the one that isn't `macros.xml`)
    - **Macros:** `macros.xml` in the same directory
    - **Assets:** scan `<command>` and `<configfiles>` for references to `.py`, `.R`, `.sh`, `.pl` scripts that exist in the directory

#### 1.2. Create macro expansion module
- **New file:** `src/bioledger/forges/toolforge/translators/galaxy_macros.py`
- **Functions:**
  - `load_macros(macros_xml_path) -> dict[str, Element]` — Parse `macros.xml`, return named macro definitions
  - `expand_xml_macros(xml_str, macros_dict) -> str` — Replace `<expand macro="name"/>` with actual XML content
  - `expand_text_macros(xml_str, known_values) -> (str, unresolved)` — Replace `@MACRO@` tokens where values are known (e.g., `@TOOL_VERSION@` from `<version>` tag), return unresolved tokens as warnings

#### 1.3. Refactor `import_galaxy_tool` to accept context
- **File:** `src/bioledger/forges/toolforge/translators/galaxy.py`
- **Change:** New signature: `import_galaxy_tool(path, use_llm=False, context=None)`
- **Pipeline:**
  ```python
  context = context or resolve_import_context(path)
  xml_str = context.xml_path.read_text()

  # Step 1: Macro expansion (programmatic, always runs)
  if context.macros_xml_path:
      macros = load_macros(context.macros_xml_path)
      xml_str = expand_xml_macros(xml_str, macros)
  xml_str, unresolved_macros = expand_text_macros(xml_str, known_values={...})
  warnings.extend(f"unresolved macro: {m}" for m in unresolved_macros)

  # Step 2: Parse (programmatic)
  result = parse_galaxy_xml(xml_str)
  warnings.extend(result.warnings)

  # Step 3: Validate (programmatic)
  validation = validate_execution(result.spec)

  # Step 4: Single LLM fix step (only if --use-llm AND something is wrong)
  has_issues = not validation.is_valid
  has_warnings = bool(warnings)
  has_residue = bool(_galaxy_residue(result.spec.command))

  if use_llm and (has_issues or has_warnings or has_residue):
      fix_result = agent.fix_galaxy_import(
          spec=result.spec,
          original_xml=xml_str,
          deps=forge_deps,
          validation_issues=validation.issues,
          parser_warnings=warnings,
          residue=_galaxy_residue(result.spec.command) if has_residue else [],
      )
      result.spec = fix_result.fixed_spec
      # re-validate after LLM fix
      validation = validate_execution(result.spec)

  # remaining_issues from LLM are surfaced to user even if validation passes
  ```

#### 1.4. Update CLI — no new flags needed
- **File:** `src/bioledger/apps/cli/main.py`
- **Change:** In `_tool_import_async()`:
  ```python
  # Before: pass file path directly to import_galaxy_tool
  # After: import_galaxy_tool(path, use_llm=use_llm)  # context resolved inside
  ```
- The existing `--use-llm`, `--dry-run`, `--name` flags work the same for files and directories

#### 1.5. Add tests
- **New test file:** `tests/test_galaxy_context.py`
- **Test cases:**
  - `test_resolve_context_file` — Single XML file returns minimal context
  - `test_resolve_context_dir` — Directory finds tool XML, macros.xml, scripts
  - `test_resolve_context_no_macros` — Directory without macros.xml
  - `test_resolve_context_detects_scripts` — Finds scripts referenced in command
- **New test file:** `tests/test_galaxy_macros.py`
- **Test cases:**
  - `test_load_macros` — Parse `macros.xml` correctly
  - `test_expand_xml_macros` — Replace `<expand>` tags with content
  - `test_expand_text_macros` — Replace `@MACRO@` tokens, report unresolved
  - `test_import_directory_with_macros` — End-to-end: import fastp directory
  - `test_import_file_unchanged` — Single file import still works as before

### Example: fastp Tool Directory

```
fastp/
├── fastp.xml          # Tool definition with <expand macro="requirements"/>
├── macros.xml         # Macro definitions (requirements, inputs, citations)
└── fastp_wrapper.py   # Python script referenced in <command>
```

```
$ bioledger tool import fastp/
[info] Found Galaxy tool: fastp.xml
[info] Found macros.xml — expanding 5 macro references
[info] Found 1 auxiliary script: fastp_wrapper.py
[ok] Parsed successfully (2 warnings: unresolved @PROFILE@, @VERSION_SUFFIX@)
[ok] Validation passed
Saved tool 'fastp' to store
```

With `--use-llm`:
```
$ bioledger tool import fastp/ --use-llm
[info] Found Galaxy tool: fastp.xml
[info] Found macros.xml — expanding 5 macro references
[info] Found 1 auxiliary script: fastp_wrapper.py
[ok] Parsed successfully (2 warnings: unresolved @PROFILE@, @VERSION_SUFFIX@)
[warn] 2 validation issues, 2 warnings, residue detected — running LLM fix…
[ok] LLM fixed 5 issues:
       - resolved @PROFILE@ to profile="24.04"
       - added version from biocontainers lookup
       - converted $input.element_identifier to Jinja2
       - added missing 'threads' parameter
       - fixed output format mismatch
[warn] 1 remaining issue (manual review needed):
       - adapter_trimming_options macro uses #for loop not yet supported
[ok] Validation passed
Saved tool 'fastp' to store
```

---

## Phase 2: Interactive Accept/Reject for LLM Changes (#17)

**Priority:** Medium  
**Estimated Effort:** 1-2 days  
**Dependencies:** None

### LLM Fix Step — Single Step, Broad Scope

The current implementation has three separate LLM steps (fix validation, fix residue, review). This is being consolidated into one:

**Before (current):**
1. `agent.fix()` — fixes validation issues only
2. `agent.fix_galaxy_import()` — fixes Galaxy residue only
3. `agent.review()` — advisory review (often catches things step 1/2 missed)

**After:**
- Single `agent.fix_galaxy_import()` call with broad mandate
- Input: spec + validation_issues + parser_warnings + residue + original_xml
- Prompt: *"Fix any issues that would stand in the way of this tool being runnable. Use validation issues as a starting point, but also address parser warnings, Galaxy/Cheetah residue, and anything else you spot. Note anything you can't fix."*
- Output: `GalaxyFixResult(fixed_spec, changes_made, remaining_issues)`
- `remaining_issues` replaces the old review step — the LLM self-reports what it couldn't resolve

This means the LLM uses validation issues as a **starting point**, not a **boundary**. It can fix things the programmatic validator didn't catch (e.g., missing parameters, incorrect command syntax, unresolved macros) and self-reports what it couldn't handle.

### Implementation Steps

#### 2.1. Add interactive prompt after LLM fix
- **File:** `src/bioledger/apps/cli/main.py`
- **Change:** In `_tool_import_async()`, after `agent.fix_galaxy_import()` returns:
  ```python
  if result.changes_made:
      console.print("[cyan]LLM proposes the following changes:[/cyan]")
      for i, change in enumerate(result.changes_made, 1):
          console.print(f"  {i}. {change}")
      
      choice = Prompt.ask(
          "Apply changes?",
          choices=["y", "n", "diff"],
          default="y",
      )
      
      if choice == "n":
          # Revert to pre-fix spec
          result.fixed_spec = original_spec
      elif choice == "diff":
          # Show diff between original and fixed spec
          show_spec_diff(original_spec, result.fixed_spec)
  ```

#### 2.2. Add spec diff utility
- **New file:** `src/bioledger/toolspec/diff.py`
- **Function:** `show_spec_diff(spec_a, spec_b, console)` — Render YAML diff

#### 2.3. Handle headless mode
- **Change:** Skip interactive prompt when `--dry-run` is set (dry-run implies non-interactive preview)

#### 2.4. Add tests
- **Test cases:**
  - `test_interactive_accept` — User accepts changes
  - `test_interactive_reject` — User rejects changes
  - `test_interactive_diff` — User requests diff view
  - `test_headless_mode` — No prompt in `--dry-run` mode

---

## Phase 3: Nextflow Import Parity

**Priority:** Medium  
**Estimated Effort:** 2-3 days  
**Dependencies:** Phase 2 (optional, for consistent UX)

### Problem

Nextflow import (`from_nextflow_module`) lacks:
- Container resolution (no biocontainers lookup)
- Warnings/forwarding system
- Residue detection
- `--dry-run` support is present but untested with LLM path

### Implementation Steps

#### 3.1. Add container resolution to Nextflow import
- **File:** `src/bioledger/forges/toolforge/translators/nextflow.py`
- **Change:** Extract container from `container` directive, run through `lookup_biocontainers_tag()`
- **Pattern:** Mirror the biocontainers-first approach from Galaxy import

#### 3.2. Add warnings system
- **Change:** Create `NextflowParseResult(dataclass)` with `spec` and `warnings` fields
- **Warnings to detect:**
  - Unresolved variables in `script` blocks
  - Missing container directive
  - Complex channel operations that may not translate

#### 3.3. Add residue detection
- **New function:** `_nextflow_residue(script_block)` — Detect Nextflow-specific constructs:
  - `$var` references that didn't convert
  - `task.attempt`, `task.cpus`, etc.
  - Channel operators (`map`, `flatten`, `collect`)

#### 3.4. Integrate with LLM pipeline
- **Change:** Apply same single-step LLM fix pattern as Galaxy import:
  - Parse → Validate → LLM fix (broad scope, only if needed) → Finalize

#### 3.5. Add tests
- **Test cases:**
  - `test_nextflow_container_resolution`
  - `test_nextflow_warnings`
  - `test_nextflow_residue_detection`
  - `test_nextflow_llm_pipeline`

---

## Phase 4: Round-Trip Completeness (#18)

**Priority:** Low  
**Estimated Effort:** 1 day  
**Dependencies:** None

### Problem

`from_galaxy_xml(to_galaxy_xml(spec))` loses information in certain fields.

### Implementation Steps

#### 4.1. Document lossy fields
- **File:** `src/bioledger/forges/toolforge/translators/galaxy.py`
- **Change:** Add docstring to `to_galaxy_xml()` documenting which fields are not preserved

#### 4.2. Add round-trip test
- **File:** `tests/test_translators.py`
- **New test:** `test_galaxy_xml_roundtrip_completeness`
- **Assertion:** Verify documented equality for non-lossy fields

#### 4.3. Consider extending Galaxy XML format (optional)
- **Idea:** Use XML comments or custom attributes to preserve lossy fields
- **Example:** `<!-- bioledger:output.pattern=*.fastq.gz -->`

---

## Phase 5: Cheetah Converter Coverage (#20)

**Priority:** Low  
**Estimated Effort:** 1-2 days  
**Dependencies:** None

### Problem

Current Cheetah → Jinja2 converter handles `#set`, `#if/elif/else/endif`, `#import` (stripped), and basic `$var` references. Missing: `#for`, `#while`, `#def`, `#echo`, `#silent`, line-continuation `\`, escaped braces.

### Implementation Steps

#### 5.1. Extend `_convert_cheetah_to_jinja2`
- **File:** `src/bioledger/forges/toolforge/translators/galaxy.py`
- **Add handlers for:**
  - `#for $item in $list ... #end for` → `{% for item in namespace.list %}...{% endfor %}`
  - `#while ... #end while` → `{% while ... %}...{% endwhile %}` (note: Jinja2 doesn't support while loops natively)
  - `#def ... #end def` → Extract as Jinja2 macro
  - `#echo $var` → `{{ namespace.var }}`
  - `#silent $var = ...` → `{% set namespace.var = ... %}`
  - Line-continuation `\` → Join lines before processing
  - Escaped braces `@{` `@}` → Preserve as literal

#### 5.2. Update residue detector
- **Change:** Remove patterns that are now handled by the converter

#### 5.3. Add tests
- **Test cases:**
  - `test_cheetah_for_loop`
  - `test_cheetah_while_loop`
  - `test_cheetah_def_macro`
  - `test_cheetah_echo`
  - `test_cheetah_silent`
  - `test_cheetah_line_continuation`
  - `test_cheetah_escaped_braces`

---

## Phase 6: Prompt Externalization (#11)

**Priority:** Low  
**Estimated Effort:** 0.5 days  
**Dependencies:** None

### Problem

35-line inline prompt in `_galaxy_fix_agent` makes prompt iteration difficult.

### Implementation Steps

#### 6.1. Create prompt file
- **New file:** `src/bioledger/forges/toolforge/prompts/galaxy_fix.md`
- **Content:** Extract the inline prompt from `_galaxy_fix_agent`

#### 6.2. Update agent to load prompt
- **File:** `src/bioledger/forges/toolforge/agent.py`
- **Change:** Replace inline prompt with `read_text()` call
- **Pattern:**
  ```python
  from pathlib import Path
  
  PROMPTS_DIR = Path(__file__).parent / "prompts"
  
  def _load_prompt(name: str) -> str:
      return (PROMPTS_DIR / f"{name}.md").read_text()
  ```

#### 6.3. Add prompt versioning (optional)
- **Idea:** Include version header in prompt files for tracking changes

---

## Dependency Graph

```
Phase 1 (Unified Import) ────────────────────────────────────┐
                                                              ├──► Phase 2 (Interactive LLM)
                                                              └──► Phase 3 (Nextflow Parity)
Phase 2 (Interactive LLM) ────────────────────────────────────────► Independent
Phase 3 (Nextflow Parity) ─────────────────────────────────────────► Independent
Phase 4 (Round-Trip) ────────────────────────────────────────┐
Phase 5 (Cheetah Coverage) ──────────────────────────────────┼──► Independent
Phase 6 (Prompt Externalization) ────────────────────────────┘
```

---

## Recommended Order

1. **Phase 1** — Unified file-or-directory import (highest impact, unlocks real-world Galaxy tools, establishes the programmatic-first pattern)
2. **Phase 6** — Prompt externalization (quick win, 0.5 days, makes prompt iteration easier for all subsequent LLM work)
3. **Phase 2** — Interactive LLM accept/reject (improves UX, builds on Phase 1's pipeline)
4. **Phase 3** — Nextflow parity (apply the same programmatic-first + gated LLM pattern)
5. **Phase 5** — Cheetah coverage (reduces LLM dependency by handling more constructs programmatically)
6. **Phase 4** — Round-trip completeness (low effort, good documentation)

---

## Risk Assessment

| Phase | Risk | Mitigation |
|-------|------|------------|
| 1. Unified Import | Macro expansion is complex; some macros may be unresolvable | Expand what's possible programmatically, pass unresolved macros + context to LLM for targeted fix |
| 1. Unified Import | Ambiguous directory (multiple tool XMLs) | Pick the XML with a `<tool>` root that isn't `macros.xml`; if multiple, prompt user |
| 2. Interactive LLM | Rich.prompt may not work in all terminals | Fallback to simple `input()` prompt |
| 3. Nextflow Parity | Nextflow syntax is more varied than Galaxy XML | Start with common patterns; use LLM for edge cases |
| 4. Round-Trip | Galaxy XML format is inherently lossy | Document limitations clearly; consider custom extensions |
| 5. Cheetah Coverage | Some Cheetah constructs have no Jinja2 equivalent | Fall back to LLM for unsupported constructs |
| 6. Prompt Externalization | Minimal risk | None needed |

---

## Testing Strategy

All phases should follow the existing test patterns:
- Unit tests for individual functions
- Integration tests for full import pipeline
- Fixture-based tests using real Galaxy tool XML files
- Round-trip tests where applicable

New test fixtures needed:
- `tests/fixtures/galaxy_tools/fastp/` — Full tool directory with `macros.xml`
- `tests/fixtures/galaxy_tools/hyphy_busted/` — Another tool with macros
- `tests/fixtures/galaxy_tools/self_contained.xml` — Simple tool without macros

---

## Migration Notes

- **Existing tool specs** will not be affected by these changes
- **Same command for files and directories** — `bioledger tool import <path>` works for both
- **No new flags** — existing `--use-llm`, `--dry-run`, `--name` flags work identically
- **Macro expansion** is automatic when `macros.xml` is detected in a directory
- **Interactive LLM** is opt-in (only triggers when `--use-llm` is set)
- **Backward compatible** — single XML file import works exactly as before
