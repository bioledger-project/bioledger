# BioLedger ToolSpec Reference

A **ToolSpec** is a YAML file that describes how to run a bioinformatics tool inside BioLedger. This document is the authoritative reference for spec authors: every field, every template variable, every validation rule is listed here.

If you just want to see a working example, look at
[`examples/hello_bioledger/line_counter.bioledger.yaml`](../../../examples/hello_bioledger/line_counter.bioledger.yaml).

---

## Table of Contents

1. [File conventions](#file-conventions)
2. [Top-level structure](#top-level-structure)
3. [ExecutionSpec (required)](#executionspec-required)
   - [Command templates](#command-templates)
   - [Inputs](#inputs)
   - [Outputs](#outputs)
   - [Parameters](#parameters)
   - [Container execution model](#container-execution-model)
4. [InterfaceSpec (optional)](#interfacespec-optional)
5. [Validation](#validation)
6. [Spec versioning & migrations](#spec-versioning--migrations)
7. [Complete reference example](#complete-reference-example)
8. [FAQ / gotchas](#faq--gotchas)

---

## File conventions

- Extension: `.bioledger.yaml` (recommended; not enforced).
- Storage: `~/.bioledger/tools/<name>.bioledger.yaml` after `bioledger tool import`.
- Encoding: UTF-8, standard YAML 1.2.
- The source of truth for the schema is
  [`src/bioledger/toolspec/models.py`](models.py). Anything here that disagrees with the Pydantic models is a doc bug — the models win.

---

## Top-level structure

```yaml
spec_version: "0.1"        # schema version, string, required
execution:                 # ExecutionSpec, required
  ...
interface:                 # InterfaceSpec, optional
  ...
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec_version` | string | no (default `"0.1"`) | Used for migrations. Keep quoted so YAML doesn't coerce it to a float. |
| `execution` | object | **yes** | The portable execution contract. |
| `interface` | object | no | UI enrichment. Completely decoupled from execution. |

---

## ExecutionSpec (required)

The minimum viable tool. Everything BioLedger needs to run the tool in a container and record the result.

```yaml
execution:
  name: samtools_sort           # required, unique identifier
  version: "1.17"               # recommended
  description: "Sort a BAM..."  # recommended
  container: "biocontainers/samtools:v1.17"   # required
  command: >-
    samtools sort -@ {{parameters.threads}}
    -o {{outputs._dir}}/sorted.bam
    {{inputs.input_bam}}
  inputs:
    input_bam:
      type: file
      format: bam
      required: true
      description: "Unsorted BAM"
  outputs:
    sorted:
      type: file
      format: bam
      pattern: "sorted.bam"
      description: "Coordinate-sorted BAM"
  parameters:
    threads:
      type: integer
      default: 4
      min: 1
      max: 64
      required: false
      description: "Number of sorting threads"
  categories:
    - alignment
    - preprocessing
  status: draft   # set by validator; don't hand-edit
```

### Top-level ExecutionSpec fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique tool identifier. Used on the CLI (`bioledger tool show <name>`) and as the filename. |
| `version` | string | no (warning if missing) | `""` | **External tool version** (e.g. `"1.20"` for samtools). Do NOT change this when editing the spec wrapper; it belongs to the upstream tool authors. |
| `description` | string | no (warning if missing) | `""` | One-sentence summary. Shown to the LLM when it picks tools. |
| `container` | string | **yes** | — | Fully qualified Docker image URI (e.g. `quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0`). |
| `command` | string | **yes** | — | Jinja2 command template. See [below](#command-templates). |
| `inputs` | map | no | `{}` | Declared input files/directories. See [Inputs](#inputs). |
| `outputs` | map | no (warning if empty) | `{}` | Declared outputs. See [Outputs](#outputs). |
| `parameters` | map | no | `{}` | Non-file parameters (threads, flags, options). See [Parameters](#parameters). |
| `categories` | list of strings | no | `[]` | Free-form tags used for grouping and LLM hints (e.g. `alignment`, `qc`, `variant_calling`). |
| `status` | enum | no | `draft` | One of `draft`, `valid`, `enriched`. Set automatically by `validate_spec`; do not hand-set. |

### Versioning fields — what to bump when

Three fields sound like "versions" but have different jobs:

| Field | What it tracks | Who changes it | Example |
|-------|----------------|----------------|---------|
| `spec_version` | The **YAML schema format** (field names, structure). Bumped when the parser or validator needs a migration. | Schema maintainers | `"0.1"` → `"0.2"` |
| `revision` | The **spec wrapper definition** (command template, inputs, outputs). Bump every time you fix or improve the wrapper, independent of the tool version. | Spec authors | `"1"` → `"2"` |
| `execution.version` | The **upstream tool's version** (samtools, GATK, etc.). Never change this unless the Docker image upgrades to a new tool release. | Tool authors | `"1.20"` → `"1.21"` |

**Rule of thumb:** when you edit a spec, bump `revision`. When the upstream tool ships a new release, bump `execution.version` (and the container). When BioLedger changes the YAML schema itself, `spec_version` is bumped for you by the schema maintainers.

### Command templates

Commands are rendered with [Jinja2](https://jinja.palletsprojects.com/). At render time, three namespaces are available:

| Template variable | Source | Rendered value |
|-------------------|--------|----------------|
| `{{inputs.<name>}}` | Declared input file | Absolute path **inside the container**: `/input/<name>/<filename>` |
| `{{parameters.<name>}}` | Declared parameter (or user override) | The parameter value, coerced by Jinja's default stringifier |
| `{{outputs._dir}}` | Always | `/output` — the directory in the container where output files must be written |

Rules:

1. **Every `{{inputs.X}}` and `{{parameters.X}}` must match a declared field**, or validation emits an ERROR.
2. `{{outputs._dir}}` is the only supported output-side template variable today. Write all outputs there; BioLedger discovers every file in `/output` after the run and records them.
3. Multi-line commands should use YAML block scalars (`>-` or `|`) so quoting stays readable.
4. The rendered string is passed through `shlex.split`; if that fails (complex shell syntax), it falls back to `sh -c "<command>"`. If you rely on pipes, redirects, or `$VAR` expansion, `sh -c` will be used automatically.

**Custom Jinja2 filters available in command templates:**

| Filter | Example input | Result | Use case |
|--------|---------------|--------|----------|
| `basename` | `/data/sample.fastq` | `sample.fastq` | Extract filename from path |
| `splitext` | `/data/sample.fastq` | `['/data/sample', '.fastq']` | Split into [root, ext] |
| `stem` | `sample.fastq.gz` | `sample.fastq` | Strip one extension |
| `stem(true)` | `reference.fna.gz` | `reference` | Strip **all** extensions (e.g. for GATK `.dict` naming) |

### Inputs

Each entry in `inputs:` is a `ToolInput`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `type` | enum | `file` | `file` or `directory`. |
| `format` | string | `"any"` | Free-form. See [well-known formats](#well-known-formats). `any` is accepted but triggers a validation warning because it makes tool chaining harder. |
| `required` | boolean | `true` | If `false`, BioLedger may omit this input from the command; your template must handle that (usually via Jinja conditionals). |
| `description` | string | `""` | Shown to the LLM and in the UI. |

At runtime, an input is mounted read-only at `/input/<name>/` inside the container. The template variable `{{inputs.<name>}}` expands to `/input/<name>/<basename>` where `<basename>` is the filename the user provided.

### Outputs

Each entry in `outputs:` is a `ToolOutput`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `type` | enum | `file` | `file` or `directory`. |
| `format` | string | `"any"` | Free-form. |
| `pattern` | string | `""` | Glob used for documentation/chaining hints (e.g. `"*.html"`). BioLedger today records **every** file produced in `/output`, so `pattern` is advisory. |
| `description` | string | `""` | Shown to the LLM and in the UI. |

After the container exits, every file in `/output` is hashed (SHA-256), sized, and added to the `LedgerEntry` as a `FileRef` with `role="output"`. There is no way to "hide" an output file short of not writing it.

### Parameters

Each entry in `parameters:` is a `ToolParameter`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `type` | enum | required | One of `string`, `integer`, `float`, `boolean`, `select`. `file` and `directory` types belong under `inputs`, not `parameters`. |
| `default` | scalar | `null` | Value used when the caller does not override. |
| `required` | boolean | `false` | If `true` and no default, the CLI/LLM must supply a value. |
| `description` | string | `""` | — |
| `min` | number | `null` | For `integer`/`float`. Default value is validated against this. |
| `max` | number | `null` | For `integer`/`float`. Default value is validated against this. |
| `options` | list of strings | `null` | Required when `type: select`; the allowed choices. |

### Container execution model

```
host filesystem                  container filesystem
---------------                  --------------------
<input_file_1_dir>/   ─read-only─►   /input/<name1>/
<input_file_2_dir>/   ─read-only─►   /input/<name2>/
<output_dir>/          ─read/write─►   /output/
```

- Each input file's **parent directory** is bind-mounted, not the individual file. If you pass a file, the whole directory is visible inside the container. Do not depend on it being empty.
- The container is invoked with `docker run --rm`, no network isolation configured here (the runner may add more; check `src/bioledger/core/containers/docker.py`).
- Working directory inside the container is whatever the image sets as `WORKDIR`. Always use absolute paths (`/input/...`, `/output/...`) in your command.
- Exit code is captured. Non-zero sets `exit_code` on the ledger entry but does not stop the file-discovery sweep — partial outputs are still recorded.

### Well-known formats

These strings are recognized without warnings:

```
fastq  fasta  bam    sam    cram   vcf    bcf    bed
gff    gtf    bigwig html   txt    csv    tsv    json
png    pdf    h5ad   tabular  any
```

Other strings are accepted (INFO-level notice only) — use them when no standard applies, e.g. `"parquet"` or `"mzml"`.

---

## InterfaceSpec (optional)

Pure UI metadata. Ignored by the executor. Use it when building forms or richer CLIs.

```yaml
interface:
  hints:
    threads:
      label: "CPU threads"
      help: "How many cores to use"
      widget: slider
      section: performance
      advanced: true
  sections:
    performance: "Performance tuning"
  conditionals:
    - param: mode
      branches:
        advanced: [threads, extra_flags]
  repeats:
    - name: extra_inputs
      title: "Additional input files"
      min: 0
      max: 10
      fields: [input_bam]
```

| Block | Purpose |
|-------|---------|
| `hints` | Per-field UI metadata: `label`, `help`, `widget`, `section`, `advanced`. `widget` values: `file`, `text`, `number`, `slider`, `select`, `checkbox`, `textarea`. |
| `sections` | Map of `section_id → display title`. Reference the id from `hints.*.section`. |
| `conditionals` | Show/hide param groups based on a controlling param (Galaxy `<conditional>`). |
| `repeats` | Allow the user to add N instances of a field group (Galaxy `<repeat>`). |

Validation only checks that every `hints` key matches a declared input or parameter.

---

## Validation

Run locally:

```bash
bioledger tool validate path/to/spec.bioledger.yaml
bioledger tool validate path/to/spec.bioledger.yaml --strict   # warnings become failures
```

Severities:

| Severity | When it fires (examples) | Effect |
|----------|--------------------------|--------|
| **ERROR** | Missing `name`/`container`/`command`; command references undeclared `inputs.X`; `default` outside `[min, max]`. | Always blocks; spec is marked `draft`. |
| **WARNING** | Missing `version`/`description`; no outputs declared; input has `format: any`; `spec_version` differs from current. | Blocks only under `--strict`. |
| **INFO** | Non–well-known format string. | Never blocks. |

After a clean validation, `status` is flipped to `valid` (or `valid` only under `--strict`, depending on mode).

---

## Spec versioning & migrations

Current schema version: **`0.1`** (see `SPEC_VERSION` in `models.py`).

- `load_spec()` reads `spec_version` and applies registered migrations in `toolspec/load.py::_migrate`.
- If no migration path exists, loading raises `ValueError`. Add migrations as pure `dict → dict` functions keyed by source version.
- Keep `spec_version` quoted (`"0.1"`) — otherwise YAML parses it as a float and versions like `"0.10"` collide with `0.1`.

---

## Complete reference example

```yaml
spec_version: "0.1"

execution:
  name: fastqc
  version: "0.12.1"
  description: "Quality control for high-throughput sequence data"
  container: "quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0"

  command: >-
    fastqc
    --outdir {{outputs._dir}}
    --threads {{parameters.threads}}
    {% if parameters.nogroup %}--nogroup{% endif %}
    {{inputs.reads}}

  inputs:
    reads:
      type: file
      format: fastq
      required: true
      description: "Input FASTQ (optionally gzipped)"

  outputs:
    html_report:
      type: file
      format: html
      pattern: "*_fastqc.html"
      description: "Per-file HTML report"
    zip_report:
      type: file
      format: any
      pattern: "*_fastqc.zip"
      description: "Zipped raw QC data"

  parameters:
    threads:
      type: integer
      default: 1
      min: 1
      max: 32
      description: "Worker threads"
    nogroup:
      type: boolean
      default: false
      description: "Disable base grouping for long reads"

  categories: [qc, preprocessing]

interface:
  hints:
    threads:
      label: "CPU threads"
      widget: slider
      section: performance
    nogroup:
      label: "Disable base grouping"
      widget: checkbox
      advanced: true
  sections:
    performance: "Performance"
```

---

## FAQ / gotchas

**Q: Can I depend on a shell feature like pipes or `$HOME`?**
Yes — `shlex.split` will fail on them, and BioLedger falls back to `sh -c "<rendered>"`. But remember: the container's shell is whatever the image provides (often `sh`, not `bash`).

**Q: How do I make an output optional?**
You can't declare it optional. Either always produce the file (even if empty) or use a Jinja conditional in the command so the tool skips generation. Any file found in `/output` will be recorded.

**Q: My tool writes to a hard-coded path, not `/output`.**
Use a post-step in the command: `&& mv /some/hardcoded/report.html {{outputs._dir}}/`.

**Q: How do I pass multiple files to one input?**
Today, each `inputs.X` is a single file or directory. For a variadic pattern, declare a `directory`-type input and have the tool glob inside it, or use an `interface.repeats` block to let the UI collect multiple values (the executor will still receive one file per declared input).

**Q: Where does `tool_spec_snapshot` come from?**
At run time, `run_tool` serializes `spec.execution` into the `LedgerEntry`. Even if you later edit the spec, historical runs keep the exact inputs/outputs/command used at the time.

**Q: Can I reuse a tool spec from Galaxy or Nextflow?**
Yes — `bioledger tool import` accepts `.xml` (Galaxy) and `.nf` (Nextflow) in addition to `.bioledger.yaml`. See `examples/galaxy_tool_import/`.
