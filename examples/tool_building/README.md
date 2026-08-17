# Example: Building Tool Specs

How to author BioLedger tool specs manually — wrapping third-party tools, custom scripts, or existing workflow engine tools. These specs live in your local store and can be used in sessions, fan-out, and crystallization.

This example covers four execution backends:

| Spec | Executor | What it shows |
|------|----------|---------------|
| `samtools_sort.bioledger.yaml` | `docker` + `conda` | Dual-executor tool — Docker auto-derives image from `conda_deps`, Conda creates native env |
| `fastqc_nextflow.bioledger.yaml` | `nextflow` | Nextflow engine executor (no container field) |
| `seqtk_galaxy.bioledger.yaml` | `galaxy` | Galaxy engine executor (runs via planemo) |

---

## Spec anatomy

Every BioLedger spec is a YAML file with `spec_version` and an `execution` block:

```yaml
spec_version: "0.1"
revision: "1"
execution:
  name: my_tool              # unique identifier
  version: "1.0"             # upstream tool version
  description: "What it does"
  container: "image:tag"     # explicit image, or omit to auto-derive from conda_deps
  executors: [docker]        # how this tool runs (docker, conda, or both)
  command: >-                # Jinja2 template
    my_tool {{inputs.data}} -o {{outputs._dir}}/result.txt
  inputs: { ... }
  outputs: { ... }
  parameters: { ... }
  categories: [utility]
```

Key template variables:
- `{{inputs.<name>}}` — mounted at `/input/<name>/<filename>` inside the container
- `{{outputs._dir}}` — always `/output` — write all outputs here
- `{{parameters.<name>}}` — user-supplied or default parameter values

See `src/bioledger/toolspec/README.md` for the full schema reference.

---

## 1. Docker + Conda (dual executor) — `samtools_sort.bioledger.yaml`

The most common case: a tool that can run via Docker or native Conda. A single
spec declares `conda_deps` and lists both executors — no explicit `container`
field is needed. Docker auto-derives the image from `conda_deps` via
[BioContainers](https://biocontainers.pro/); Conda creates a native environment
and runs via `conda run`.

```yaml
execution:
  name: samtools_sort
  # No explicit container — Docker auto-derives from conda_deps via BioContainers
  executors: [docker, conda]
  conda_deps:
    - "samtools=1.17"
  command: >-
    samtools sort -@ {{parameters.threads}}
    -o {{outputs._dir}}/sorted.bam
    {{inputs.input_bam}}
```

**Key points:**
- `executors: [docker, conda]` — BioLedger picks the available backend at runtime (Docker if installed, else Conda)
- `conda_deps` is the single source of truth for the tool's dependencies — Docker derives the image, Conda builds the env
- Omit `container` to let Docker auto-derive; set it explicitly only if you need a specific image tag
- Conda environments are named deterministically (`bioledger_<hash>`) and cached for reuse
- If `mamba` is on PATH, it's used instead of `conda` for faster env solves
- BioLedger runs Docker via `docker run --rm` with inputs mounted at `/input/` and outputs collected from `/output/`

**Import and validate:**
```bash
bioledger tool import examples/tool_building/samtools_sort.bioledger.yaml
bioledger tool validate ~/.bioledger/tools/samtools_sort.bioledger.yaml
```

**Run in a session:**
```
you> run samtools_sort on the bam file
assistant> Submitted samtools_sort as job abc123 (executor: docker).
```

---

## 2. Nextflow executor — `fastqc_nextflow.bioledger.yaml`

For tools that should run via the Nextflow workflow engine. BioLedger generates a Nextflow DSL2 process from the spec using `to_nextflow_process()`.

```yaml
execution:
  name: fastqc_nextflow
  executors: [nextflow]
  # no container — the Nextflow engine manages its own environment
  command: >-
    fastqc {{inputs.reads}} -o {{outputs._dir}} -t {{parameters.threads}}
```

**Key points:**
- `container` and `conda_deps` are **not allowed** when `executors` contains only engine types (`nextflow`, `snakemake`, `galaxy`) — the engine manages its own environment
- BioLedger translates the `command`, `inputs`, `outputs`, and `parameters` into a Nextflow process block with proper `input:`, `output:`, and `script:` sections
- At runtime, the Nextflow adapter generates a complete `main.nf` with channels wired from `input_mapping` and runs `nextflow run`
- Requires `bioledger[nextflow]` — install Nextflow separately

**Import and validate:**
```bash
bioledger tool import examples/tool_building/fastqc_nextflow.bioledger.yaml
bioledger tool validate ~/.bioledger/tools/fastqc_nextflow.bioledger.yaml
```

**Run in a session:**
```
you> run fastqc_nextflow on the raw reads
assistant> Submitted fastqc_nextflow as job abc123 (engine: nextflow).
```

---

## 3. Galaxy executor — `seqtk_galaxy.bioledger.yaml`

For tools that should run via Galaxy (local planemo or remote Galaxy instance).

**Discovered tools** (from Tool Shed search): BioLedger generates a `.ga` workflow
and submits it via planemo with the downloaded tool XML.

**User-defined specs**: The `command` field must contain a valid Galaxy/planemo
invocation (e.g. `planemo run tool.xml --input ...`). BioLedger runs this
directly as a subprocess.

```yaml
execution:
  name: seqtk_galaxy
  executors: [galaxy]
  # no container — Galaxy manages its own tool environment
  command: >-
    planemo run {{inputs.tool_xml}} --input {{inputs.reads}} --output {{outputs._dir}}/output.fa
  inputs:
    reads:
      type: file
      format: fastq
      required: true
    tool_xml:
      type: file
      format: xml
      required: true
      description: "Galaxy tool XML wrapper"
```

**Key points:**
- Like Nextflow, `container` and `conda_deps` are **not allowed** — Galaxy manages its own tool environment
- For **discovered tools** (Tool Shed), BioLedger generates a `.ga` workflow and submits it via planemo with `--tool_dir`
- For **user-defined specs**, the `command` field must be a valid planemo/Galaxy invocation — BioLedger runs it directly as a subprocess
- At runtime, the Galaxy adapter submits to a local planemo instance or a remote Galaxy server (configured in `engines.galaxy`)
- Requires `bioledger[galaxy]` — install planemo separately

**Import and validate:**
```bash
bioledger tool import examples/tool_building/seqtk_galaxy.bioledger.yaml
bioledger tool validate ~/.bioledger/tools/seqtk_galaxy.bioledger.yaml
```

**Run in a session:**
```
you> run seqtk_galaxy on the raw reads
assistant> Submitted seqtk_galaxy as job abc123 (engine: galaxy).
```

---

## Wrapping native workflow engines

The examples above show single-tool specs. You can also wrap an **existing
workflow** (a local Snakefile, an nf-core pipeline, or a planemo/Galaxy tool)
by authoring a toolspec whose `command` field invokes the native engine.
There is no separate "workflow file" field — the `command` field *is* the
wrapper. Inputs and outputs declared on the spec are used for staging,
provenance, and crystallize channel wiring, just like any other spec.

### Local Snakefile

```yaml
execution:
  name: my_snakemake_workflow
  executors: [snakemake]
  command: >-
    snakemake -s Snakefile --configfile config.yaml
  inputs:
    config:
      type: file
      format: yaml
      required: true
      description: "Snakemake config file"
  outputs:
    results:
      pattern: "results/*.txt"
```

The `command` is the engine invocation. If your Snakefile takes input files
as config values, pass them via Jinja:

```yaml
  command: >-
    snakemake -s Snakefile --config input_bam={{inputs.input_bam}} --outdir {{outputs._dir}}
```

### nf-core workflow

```yaml
execution:
  name: rnaseq_pipeline
  executors: [nextflow]
  command: >-
    nextflow run nf-core/rnaseq -r 3.14.0
    --input {{inputs.samplesheet}}
    --outdir {{outputs._dir}}
  inputs:
    samplesheet:
      type: file
      format: csv
      required: true
```

Note: for **discovered** nf-core modules, `submit_discovered` handles the
wrapping automatically (see [Tool discovery](../tool_discovery/)). This
example is for wrapping a whole nf-core *pipeline* by hand.

### planemo/Galaxy tool

```yaml
execution:
  name: my_galaxy_tool
  executors: [galaxy]
  command: >-
    planemo run {{inputs.tool_xml}}
    --input {{inputs.input_dataset}}
    --output {{outputs._dir}}/out.bam
  inputs:
    tool_xml:
      type: file
      format: xml
      required: true
      description: "Galaxy tool XML wrapper"
    input_dataset:
      type: file
      required: true
```

### Snakemake discovery asymmetry

Snakemake does not have a first-class tool discovery registry analogous to
the Galaxy Tool Shed or nf-core modules. To use a snakemake wrapper or a
local Snakefile, author a toolspec by hand as shown above. (A future release
may add discovery for [snakemake-wrappers](https://github.com/snakemake/snakemake-wrappers).)

---

## Validation rules by executor

| Rule | docker | conda | nextflow/snakemake/galaxy |
|------|--------|-------|---------------------------|
| `container` required? | No — auto-derived from `conda_deps` if omitted | No (runs via conda env from `conda_deps`) | No (not allowed) |
| `conda_deps` required? | Only if `container` is omitted | Yes | No (not allowed) |
| Can combine with conda? | Yes — `executors: [docker, conda]` | Yes | No (engine executors are exclusive) |
| At most one engine executor? | — | — | Yes (e.g. cannot have both `nextflow` and `galaxy`) |

Run `bioledger tool validate <path> --strict` to catch warnings too.

---

## Using imported specs

Once imported, specs are available in sessions and for fan-out:

```bash
# List all local tools
bioledger tool list

# Show details
bioledger tool show samtools_sort

# Run in a session
bioledger session new my-analysis
bioledger resume my-analysis
# > run samtools_sort on the bam file
```

---

## Future: LLM-assisted spec authoring

The `ToolForgeAgent` has `parse`, `fix`, and `review` methods that can help draft specs from natural language descriptions, repair validation issues, and review specs for conceptual correctness. A future CLI command will let you describe a tool in plain English and have the agent generate a spec. For now, specs are authored manually as shown above.

---

## See also

- [Tool discovery](../tool_discovery/) — search external repositories and run discovered tools
- [Hello World](../hello_bioledger/) — end-to-end walkthrough with a simple custom spec
- [Tool spec schema](../../src/bioledger/toolspec/README.md) — full field reference and validation rules
- [Cluster execution](../cluster/) — guide and example configs for Slurm, Singularity, and remote Galaxy

---

## Notes on crystallized workflows

- **Unified workflow:** All sessions (single or multi-engine) crystallize into a single `workflow.nf` as the RO-Crate mainEntity. Engine entries (snakemake/galaxy/nextflow) appear as Nextflow processes without container directives.
- **Profile-based config:** The generated `nextflow.config` uses Nextflow profiles for container runtime selection (`-profile docker`, `-profile singularity`, `-profile conda`). The profile matching the original execution is uncommented by default.
- **Engine environment:** When engine entries are present, an `engine-env.yml` conda environment file is generated with pinned engine versions for reproducibility.
- **Supplementary files:** Engine-specific files (`.smk` for Snakemake, tool XML for Galaxy, nf-core modules for Nextflow) are included as supplementary files in the crate.
- Docker steps in crystallized workflows port cleanly across container tech (Docker, apptainer/Singularity) and schedulers via Nextflow's config
- Conda steps emit a Nextflow `conda` directive — the profile-based config enables `conda.enabled = true` when conda steps are present
- Created conda environments (`bioledger_<hash>`) are cached and reused across runs but never automatically removed by BioLedger — same as Docker images, which also accumulate in the local image cache. Manual cleanup via `conda env remove -n <name>` if disk space matters.
