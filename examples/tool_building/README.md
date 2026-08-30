# Example: Building Tool Specs

How to author BioLedger tool specs manually — wrapping third-party tools, custom scripts, or existing workflow engine tools. These specs live in your local store and can be used in sessions, fan-out, and crystallization.

> **Before you write a spec:** BioLedger can automatically discover and run tools from the [Galaxy Tool Shed](https://toolshed.g2.bx.psu.edu/) (any repo, any owner) and [nf-core modules](https://nf-co.re/modules) — no spec needed. Use `bioledger tool search` and the agent's `search_toolshed` / `search_nfcore` tools to find and run them directly. Hand-authored specs are for:
> - **Individual tools** (docker/conda) — the most common case
> - **Non-nf-core Nextflow workflows** — nf-core *modules* are auto-discoverable, but whole pipelines from other orgs (e.g. veg/CAPHEINE) are not
> - **Pinning a specific Tool Shed revision** — auto-discovery always fetches the latest; a spec lets you pin a changeset for reproducibility
> - **Custom/local workflows** — Snakefiles, lab-internal Galaxy tools, etc.
>
> See [Tool discovery](../tool_discovery/) for the auto-discovery flow.

This example covers four execution backends:

| Spec | Executor | What it shows |
|------|----------|---------------|
| `samtools_sort.bioledger.yaml` | `docker` + `conda` | Dual-executor tool — Docker auto-derives image from `conda_deps`, Conda creates native env |
| `capheine_nextflow.bioledger.yaml` | `nextflow` | Wrapping a non-nf-core Nextflow workflow (veg/CAPHEINE) |
| `toolshed_agat.bioledger.yaml` | `galaxy` | Pinning a specific Tool Shed revision (bgruening/agat @ 6d8be3337e2a) |

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

## 2. Nextflow executor — `capheine_nextflow.bioledger.yaml`

Engine executors (`nextflow`, `snakemake`, `galaxy`) are for wrapping **existing
workflows** — not individual tools. If you want to run fastqc, write a docker/conda
spec (see section 1). If you want to run an nf-core pipeline or module, use
`bioledger tool search` — it's auto-discoverable. This example wraps
[CAPHEINE](https://github.com/veg/CAPHEINE), a real non-nf-core Nextflow workflow
from the veg GitHub org for automated evolutionary selection analysis:

```yaml
execution:
  name: capheine
  executors: [nextflow]
  # No container/conda_deps — Nextflow manages its own environment via profiles
  command: >-
    nextflow run veg/CAPHEINE
    --reference_genes {{inputs.reference_genes}}
    --unaligned_seqs {{inputs.unaligned_seqs}}
    --outdir {{outputs._dir}}
    -profile docker
```

**Key points:**
- `container` and `conda_deps` are **not allowed** when `executors` contains only engine types (`nextflow`, `snakemake`, `galaxy`) — the engine manages its own environment
- The `command` field is the full `nextflow run` invocation — BioLedger stages inputs and runs it as a subprocess
- nf-core **modules** are auto-discoverable via `bioledger tool search`. nf-core **pipelines** (e.g. `nf-core/rnaseq`) and non-nf-core workflows (like CAPHEINE) are not — you need a spec like this for those
- Requires `bioledger[nextflow]` — install Nextflow separately

**Import and validate:**
```bash
bioledger tool import examples/tool_building/capheine_nextflow.bioledger.yaml
bioledger tool validate ~/.bioledger/tools/capheine_nextflow.bioledger.yaml
```

**Run in a session:**
```
you> run capheine on the pathogen sequences with the reference genes
assistant> Submitted capheine as job abc123 (engine: nextflow).
```

---

## 3. Galaxy executor — `toolshed_agat.bioledger.yaml`

Wrapping a specific versioned tool from the [Galaxy Tool Shed](https://toolshed.g2.bx.psu.edu/).
This example wraps [AGAT](https://github.com/NBISweden/AGAT) from the `bgruening/agat`
repo at a pinned changeset revision (`6d8be3337e2a`, numeric revision 7). The
`command` field downloads the tool XML at that revision and runs it via `planemo run`:

```yaml
execution:
  name: toolshed_agat
  version: "1.7.0+galaxy0"
  executors: [galaxy]
  # No container/conda_deps — Galaxy/planemo manages its own tool environment
  command: >-
    curl -sL "https://toolshed.g2.bx.psu.edu/repos/bgruening/agat/raw/6d8be3337e2a/agat.xml"
    -o {{outputs._dir}}/agat.xml &&
    planemo run {{outputs._dir}}/agat.xml
    --input {{inputs.gff}}
    --output {{outputs._dir}}/output.gff3
  inputs:
    gff:
      type: file
      format: gff
      required: true
```

**Key points:**
- Like Nextflow, `container` and `conda_deps` are **not allowed** — Galaxy manages its own tool environment
- The `command` field pins a specific Tool Shed changeset revision (`6d8be3337e2a`) — this ensures reproducibility. The Tool Shed repo can change at any time, but this spec always fetches the same version
- The raw download URL pattern is: `https://toolshed.g2.bx.psu.edu/repos/<owner>/<repo>/raw/<changeset_revision>/<tool_xml>`
- **Any** Tool Shed repo (any owner — iuc, bgruening, devteam, etc.) is auto-discoverable via `bioledger tool search`. You'd hand-author a spec like this when you need to **pin a specific changeset revision** for reproducibility — auto-discovery always fetches the latest
- At runtime, the Galaxy adapter runs the command directly as a subprocess
- Requires `bioledger[galaxy]` — install planemo separately

**Import and validate:**
```bash
bioledger tool import examples/tool_building/toolshed_agat.bioledger.yaml
bioledger tool validate ~/.bioledger/tools/toolshed_agat.bioledger.yaml
```

**Run in a session:**
```
you> run toolshed_agat on the gff file
assistant> Submitted toolshed_agat as job abc123 (engine: galaxy).
```

---

## Wrapping native workflow engines

Sections 2 and 3 show concrete examples of wrapping nf-core and planemo/Galaxy
tools. The same pattern works for any existing workflow — a local Snakefile, a
WDL pipeline, or a custom Nextflow workflow. The `command` field *is* the
wrapper: it invokes the native engine, and BioLedger stages inputs, records
provenance, and wires crystallize channels around it.

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
