# Hello World: End-to-End Walkthrough

This walkthrough loads an ISA-Tab dataset, imports a simple custom tool, runs it in a session, and packages the result. You'll need an LLM API key set (see the main [README](../../README.md#prerequisites)) and Docker running.

All example files are included in this directory — follow along without creating anything from scratch.

## 1. Set up

```bash
# From the repo root
pip install -e ".[cli,toolforge,analysis,crateforge]"
cp .env.example .env
# Edit .env with your API key
```

## 2. Explore the example files

```
hello_bioledger/
  data/                          # ISA-Tab dataset
    i_investigation.txt          # investigation metadata
    s_study.txt                  # study: sources → samples with organism
    a_assay.txt                  # assay: samples → data files
  line_counter.bioledger.yaml    # tool spec
```

### The ISA-Tab dataset

The `data/` directory is a valid ISA-Tab archive describing three samples across two organisms:

**`s_study.txt`** maps biological sources to samples with ontology-backed organism annotations:

| Source Name | Characteristics[Organism] | Term Source REF | Sample Name |
|-------------|--------------------------|-----------------|-------------|
| source_S001 | Mus musculus | NCBITAXON | S001 |
| source_S002 | Mus musculus | NCBITAXON | S002 |
| source_S003 | Homo sapiens | NCBITAXON | S003 |

**`a_assay.txt`** describes the sequencing assay that produced FASTQ files:

| Sample Name | Protocol REF | Raw Data File |
|-------------|-------------|---------------|
| S001 | nucleotide sequencing | S001_R1.fastq.gz |
| S002 | nucleotide sequencing | S002_R1.fastq.gz |
| S003 | nucleotide sequencing | S003_R1.fastq.gz |

The ISA-Tab records the full experimental context: biological sources, derived samples, the assay type (nucleotide sequencing), and the resulting data files. This structured metadata is the foundation of BioLedger's provenance tracking.

> **Why ISA-Tab?** It gives BioLedger structured metadata to reason about — organisms, assay types, ontology terms — which improves tool suggestions and makes the final RO-Crate self-describing.

### The tool spec

`line_counter.bioledger.yaml` defines a trivial tool that counts lines in a text file (like an ISA-Tab study file). We're writing this by hand for the tutorial, but in practice **ToolForge can help** — import a Nextflow module (`bioledger tool import trimmomatic.nf`), search the Galaxy Tool Shed and nf-core modules (`bioledger tool search "fastqc"`), or ask the LLM to draft one from scratch in an interactive session. See the [tool discovery & import example](../galaxy_tool_import/) for a walkthrough.

## 3. Import and validate the tool

```bash
bioledger tool import examples/hello_bioledger/line_counter.bioledger.yaml
# ✓ Imported 'line_counter' → ~/.bioledger/tools/line_counter.bioledger.yaml

bioledger tool validate ~/.bioledger/tools/line_counter.bioledger.yaml
# ✓ line_counter is valid

bioledger tool show line_counter
# line_counter  v1.0
#   Container: python:3.11-slim
#   Inputs:  input_file (tsv, required)
#   Outputs: summary (json)
```

## 4. Create a session and load the ISA-Tab dataset

```bash
bioledger session new --name "hello world" --description "Testing line_counter on ISA-Tab"
# Session a1b2c3d4 created

bioledger resume a1b2c3d4
```

Inside the interactive session, load the ISA-Tab directory:

```
you> load examples/hello_bioledger/data/

Loaded dataset "Sample metadata analysis"
  Samples: 3
  Organisms: Mus musculus, Homo sapiens
  Assay type: nucleotide sequencing
  File formats: fastq
  Files: 3

assistant> I see an ISA-Tab dataset with 3 samples across 2 organisms.
           What would you like to do?
```

BioLedger parsed the ISA-Tab, extracted organisms and assay type from the structured metadata, and recorded a `data_import` entry in the ledger.

## 5. Run the tool

Let's analyze the ISA-Tab study file with our `line_counter` tool:

```
you> run line_counter on s_study.txt
assistant> Suggested: line_counter
           Params: {}
           Run this tool? [y/N]: y
assistant> line_counter completed. Outputs: [summary.json]
```

The ledger now has two entries chained together — a `data_import` (the ISA-Tab dataset) and a `tool_run` (our line counting analysis) whose `parent_id` points back to the import:

```
you> review
  DATA [e1f2a3b4] data_import: Loaded dataset: Sample metadata analysis
  TOOL [c5d6e7f8] tool_run: line_counter  outputs=[summary.json]
```

## 6. Crystallize and package

Still inside the session, or after quitting:

```bash
# Generate a Nextflow workflow from the session
bioledger crystallize a1b2c3d4
# nextflow.enable.dsl=2
#
# process LINE_COUNTER {
#     container 'python:3.11-slim'
#     input: path(input_file)
#     output: path('summary.json')
#     script: ...
# }

# Bundle everything into an RO-Crate
bioledger package a1b2c3d4
# RO-Crate written to ~/.bioledger/crates/a1b2c3d4
#   Entries: 2
#   Includes: workflow.nf, ISA-Tab files, ledger.json
```

The crate at `~/.bioledger/crates/a1b2c3d4/` now contains:

- `ro-crate-metadata.json` — full provenance graph
- `workflow.nf` — crystallized Nextflow workflow
- `nextflow.config` — Docker configuration for reproducible runs
- `ledger.json` — raw ledger entries
- ISA-Tab files (`i_investigation.txt`, `s_study.txt`, `a_assay.txt`)
- Tool output (`summary.json`)

### 6.1 Validate the packaged workflow

Run the workflow from the crate to verify everything works:

```bash
cd ~/.bioledger/crates/a1b2c3d4
nextflow run workflow.nf
```

Expected output:

```
executor >  local (1)
[xx/xxxxxx] process > LINE_COUNTER [100%] 1 of 1 ✔
```

Check the reproduced results:

```bash
cat results/LINE_COUNTER/summary.json
# {"file": "s_study.txt", "line_count": 4}
```

The workflow runs each tool in its original container (configured via `nextflow.config`), and `publishDir` ensures results land in `results/` for easy inspection. This validates that the crate captures a fully reproducible analysis.

## 7. What just happened?

You went from an ISA-Tab dataset to a reproducible, packaged analysis without ever writing workflow syntax:

| Step | Ledger Entry | What was captured |
|------|-------------|-------------------|
| `load data/` | `data_import` | ISA-Tab source, organisms, assay type, file paths with SHA-256 hashes |
| `run line_counter` | `tool_run` | Container image, command, input/output files with hashes, exit code, duration, frozen tool spec |

The `tool_run` entry's `parent_id` links back to the `data_import`, forming a DAG that BioLedger walks to produce the Nextflow workflow.

## Next steps

- **Import real tools**: see the [Galaxy tool import example](../galaxy_tool_import/) for importing FastQC, Trimmomatic, etc.
- **Chain tools**: run multiple tools in sequence — BioLedger auto-chains them via `parent_id`
- **Export to Galaxy**: `bioledger crystallize <id> --format galaxy`
- **Start from CSV**: have a CSV samplesheet instead of ISA-Tab? See the [CSV to ISA-Tab example](../csv_to_isatab/) — BioLedger converts it automatically
