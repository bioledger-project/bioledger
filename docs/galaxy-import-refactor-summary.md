# Galaxy XML Import Refactor — Implementation Summary

**Date:** 2026-04-26  
**Status:** Core refactor complete; 128/128 tests passing

---

## ✅ Implemented (High Priority)

### 1. Final Validation in LLM Import Path (#1)
- **Problem:** `--use-llm` import saved tools without showing validation issues.
- **Fix:** Added `validate_spec()` call after LLM pipeline, with shared `print_validation_issues()` helper.
- **Files:** `src/bioledger/apps/cli/main.py`, `src/bioledger/apps/cli/_ui.py`

### 2. Shared Container Resolution Module (#2, #6, #8, #12)
- **Changes:**
  - Extracted biocontainers lookup to `src/bioledger/toolspec/containers.py`
  - Replaced `requests` with `httpx` + `asyncio.to_thread()` for async compatibility
  - Added `functools.lru_cache(maxsize=256)` to avoid repeated quay.io hits
  - Removed bogus `hdfd78af_0` fallback (now returns `None`/empty string on failure)
  - Improved version sorting with `packaging.version` fallback
- **API:**
  - `lookup_biocontainers_tag_sync(pkg, version)` — sync, cached
  - `lookup_biocontainers_tag(pkg, version)` — async wrapper
  - `has_macro(value)` — macro detection (now case-insensitive: `[A-Za-z_]`)

### 3. Biocontainers-First Priority (#8)
- **Before:** `<container>` tag took precedence over `<requirement type="package">`
- **After:** Package requirement → biocontainers lookup runs first; explicit container only used as fallback
- **Warning:** When explicit container is overridden, user sees:  
  `"explicit container 'X' overridden by biocontainers resolution 'Y' for reproducibility"`

### 4. Pure Parser with Warnings (#4)
- **New:** `parse_galaxy_xml(xml_str) -> GalaxyParseResult(spec, warnings)`
- **Back-compat:** `from_galaxy_xml(xml_str) -> ExecutionSpec` still prints warnings directly
- **Benefit:** Orchestrator can forward warnings to LLM and control console output

### 5. Gated LLM Steps (#3)
- **Before:** All LLM steps ran unconditionally when `--use-llm` was set.
- **After:**
  - `agent.fix()` only runs if `not result.is_valid`
  - `agent.fix_galaxy_import()` only runs if residue detected OR validation still failing
  - `agent.review()` runs but output is clearly labelled "advisory"
- **Residue Detection:** `_galaxy_residue(command)` scans for:
  - Bare `$var` references
  - Cheetah directives (`#set`, `#if`, etc.)
  - Galaxy attrs (`.element_identifier`, `.files_path`, `.dataset`)
  - Python calls (`re.sub`, `.endswith` on templates)

### 6. Forward Warnings to LLM (#5)
- **New signature:** `agent.fix_galaxy_import(..., parser_warnings, residue)`
- **Prompt context:** LLM now sees:
  ```
  Programmatic parser warnings:
    - container: @DOCKER_IMAGE@
    - version: @TOOL_VERSION@
  Cheetah/Galaxy residue detected in command:
    - bare $var reference
    - .element_identifier
  ```

### 7. Structured LLM Output (#9, #10)
- **New pydantic model:** `GalaxyFixResult`
  - `fixed_spec: ExecutionSpecDraft` — full spec so LLM can add parameters
  - `changes_made: list[str]`
  - `remaining_issues: list[str]`
- **Removed:** ~40 lines of string-parsing (`CHANGES_MADE:`, `REMAINING_ISSUES:`, `FIXED_COMMAND:`)

### 8. CLI `--dry-run` (#14)
- **Usage:** `bioledger tool import tool.xml --dry-run`
- **Behavior:** Renders resulting YAML to stdout, runs validation, prints issues, **does not save**
- **Helper:** `dump_spec_yaml(spec)` in `toolspec/load.py`

### 9. Progress Spinners (#15)
- **Wrapped LLM calls:**
  - `console.status("[cyan]LLM fallback parse…[/cyan]")`
  - `console.status("[cyan]LLM fixing validation issues…[/cyan]")`
  - `console.status("[cyan]LLM repairing Galaxy-specific constructs…[/cyan]")`
  - `console.status("[cyan]LLM conceptual review…[/cyan]")`

### 10. Cleanups (#10, #19)
- Merged duplicate docstrings in `_convert_cheetah_to_jinja2`
- Removed redundant `import re` inside functions
- Widened `_has_macro` regex to `[A-Za-z_]` (was `[A-Z_]`)

---

## ⏳ Outstanding / Future Work

### 1. Interactive Accept/Reject for LLM Changes (#17)
**Priority:** Medium  
**Idea:** After `fix_galaxy_import` returns `changes_made`, prompt user:
```
Apply 3 LLM fixes? [Y/n/diff]:
  1. ✓ Replace $input.element_identifier with {{ inputs.input | basename }}
  2. ✓ Add 'threads' parameter for ${GALAXY_SLOTS:-4}
  3. ✓ Convert .endswith('.gz') to Jinja2 conditional
```
**Blocker:** Requires `rich.prompt` or `inquirer` dependency; needs UX design for `--use-llm` headless mode.

### 2. Round-Trip Completeness (#18)
**Priority:** Low  
**Issue:** `from_galaxy_xml(to_galaxy_xml(spec))` loses:
- `output.pattern` (not stored in Galaxy XML `<data>`)
- `param.required` for non-data params
- `input.format` multi-values (only first kept)
- `spec.categories`, `input.description` beyond Galaxy `label`

**Action:** Document lossy fields in `to_galaxy_xml` docstring; add round-trip test that asserts documented equality.

### 3. Cheetah Converter Coverage (#20)
**Priority:** Low  
**Current:** Handles `#set`, `#if/elif/else/endif`, `#import` (stripped).  
**Missing:** `#for`, `#while`, `#def`, `#echo`, `#silent`, line-continuation `\`, escaped braces.  
**Mitigation:** Residue detector catches these and triggers LLM fix step.

### 4. Prompt Externalization (#11 — Optional)
**Priority:** Low  
**Current:** 35-line inline prompt in `_galaxy_fix_agent`.  
**Proposal:** Move to `src/bioledger/forges/toolforge/prompts/galaxy_fix.md` and `read_text()` on first use.  
**Benefit:** Easier to iterate on prompts without Python edits; could support prompt versioning.

### 5. Nextflow Import Parity
**Priority:** Medium  
**Gap:** Nextflow import (`from_nextflow_module`) lacks:
- Container resolution (no biocontainers lookup)
- Warnings/forwarding system
- Residue detection
- `--dry-run` support is present but untested with LLM path

**Action:** Apply same container module + gating pattern to `nextflow.py` translator.

---

## Verification Checklist

| Check | Status |
|-------|--------|
| All 128 tests pass | ✅ |
| Tool YAMLs load (dict format) | ✅ |
| `--dry-run` renders YAML, no save | ✅ |
| `--use-llm` with valid XML (no residue) skips fix step | ✅ |
| `--use-llm` with residue triggers fix + shows spinner | ✅ |
| Failed biocontainers lookup shows warning + validation error | ✅ |
| Successful biocontainers lookup resolves real tag (e.g., `fastqc:0.12.1--hdfd78af_0`) | ✅ |
| Explicit container overridden by biocontainers when package req present | ✅ |
| `from_galaxy_xml` back-compat prints warnings | ✅ |
| `parse_galaxy_xml` pure, no side effects | ✅ |

---

## Files Changed Summary

| File | Change |
|------|--------|
| `src/bioledger/toolspec/containers.py` | **New** — shared biocontainers + macro detection |
| `src/bioledger/apps/cli/_ui.py` | **New** — shared validation issue printer |
| `src/bioledger/apps/cli/main.py` | Added `--dry-run`, progress spinners, `_finalize_tool_import` helper |
| `src/bioledger/toolspec/load.py` | Added `dump_spec_yaml()` helper |
| `src/bioledger/forges/toolforge/agent.py` | `GalaxyFixResult` structured output, context-aware prompts |
| `src/bioledger/forges/toolforge/translators/galaxy.py` | Pure parser, residue gating, spinner wrappers, container refactor |
| `tests/conftest.py` | Fixture converted to dict format |
| `tests/test_*.py` | Multiple test files converted to dict format |
| `~/.bioledger/tools/*.yaml` | User tool specs converted to dict format |

---

## Migration Notes for Users

1. **Old tool YAMLs (list format)** still load but new saves use dict format.
2. **Container resolution** now prefers biocontainers; check imports if you relied on explicit `<container>` tags.
3. **Failed biocontainers lookup** now surfaces as validation error rather than silent fallback.
4. **Use `--dry-run`** to preview imports before committing to store.
