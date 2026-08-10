# Example: Tool Discovery & Import

BioLedger can discover tools from external repositories — the [Galaxy Tool Shed](https://toolshed.g2.bx.psu.edu/) and [nf-core modules](https://nf-co.re/modules) — and import Nextflow modules or BioLedger YAML specs. Tool specs can be exported to either Galaxy XML or Nextflow DSL2.

## Files in this directory

- **`trimmomatic.nf`** — Example Nextflow DSL2 process for import (Trimmomatic)
- **`isatab/`** — Complete ISA-Tab dataset directory with synthetic test data:
  - `i_investigation.txt` — Investigation metadata
  - `s_study.txt` — Study with one sample
  - `a_assay.txt` — Sequencing assay pointing to the FASTQ
  - `sample.fastq` — Tiny synthetic FASTQ file (4 reads)

## Search for tools

Use `bioledger tool search` to find tools in the Galaxy Tool Shed and nf-core modules:

```bash
# Search both sources
bioledger tool search "fastqc"

# Search only the Galaxy Tool Shed
bioledger tool search "bwa" --source toolshed

# Search only nf-core modules
bioledger tool search "samtools" --source nfcore
```

Results show tool name, description, and repository information.

## Import a Nextflow module

```bash
bioledger tool import examples/galaxy_tool_import/trimmomatic.nf
# ✓ Imported 'trimmomatic' → ~/.bioledger/tools/trimmomatic.bioledger.yaml
```

## Import a BioLedger YAML spec

```bash
bioledger tool import specs/samtools_sort.bioledger.yaml
```

## LLM-enhanced import (optional)

For complex Nextflow modules, use `--use-llm` to get additional analysis and fixes:

```bash
# Requires an LLM API key (OpenAI, Google, Anthropic, or Azure)
export OPENAI_API_KEY="your-key-here"

# Import with LLM assistance
bioledger tool import examples/galaxy_tool_import/trimmomatic.nf --use-llm
```

## Export to Galaxy XML or Nextflow DSL2

Tool specs are format-agnostic. Export to either platform:

```bash
# To Galaxy XML
bioledger tool export trimmomatic --format galaxy -o trimmomatic.xml

# To Nextflow DSL2
bioledger tool export trimmomatic --format nextflow -o trimmomatic.nf
```

## Run discovered tools in a session

Inside an interactive session, the agent can search external repositories and run discovered tools directly:

```bash
bioledger session new tool-discovery-test
bioledger resume tool-discovery-test
```

```
you> load examples/galaxy_tool_import/isatab/
Loaded dataset "Sample FastQC Test Dataset"
  Samples: 1
  Organisms: Homo sapiens
  File formats: fastq

you> search the tool shed for fastqc
assistant> Tool Shed results:
  fastqc: FastQC — Read Quality reports (repo: iuc/fastqc)

you> run the fastqc tool from the tool shed on the sample
assistant> Submitted discovered tool fastqc as job abc123.
           Source: toolshed:iuc/fastqc. Type 'status' to check progress.
```

Discovered tools are downloaded, parsed into an ExecutionSpec, and submitted to the appropriate engine:
- **Galaxy Tool Shed tools** → run via planemo (local Galaxy instance)
- **nf-core modules** → run via Nextflow

## See also

- [Hello World](../hello_bioledger/) — end-to-end walkthrough with ISA-Tab
- [CSV to ISA-Tab](../csv_to_isatab/) — converting samplesheets to structured metadata
- [Variant calling](../variant_calling/) — full pipeline using the public library repos
