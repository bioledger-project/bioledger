# Example: Tool Discovery, Import & Export

BioLedger can discover tools from external repositories and run them with their native workflow engines — no translation to BioLedger specs required. It can also import Nextflow modules as BioLedger specs, and export BioLedger specs to Galaxy XML or Nextflow DSL2.

## Files in this directory

- **`trimmomatic.nf`** — Example Nextflow DSL2 process for import (Trimmomatic)
- **`isatab/`** — Complete ISA-Tab dataset directory with synthetic test data:
  - `i_investigation.txt` — Investigation metadata
  - `s_study.txt` — Study with one sample
  - `a_assay.txt` — Sequencing assay pointing to the FASTQ
  - `sample.fastq` — Tiny synthetic FASTQ file (4 reads)

---

## Tool Discovery: Search → Download → Run

BioLedger searches the [Galaxy Tool Shed](https://toolshed.g2.bx.psu.edu/) and [nf-core modules](https://nf-co.re/modules) for tools that aren't installed locally. Discovered tools are downloaded and run via their **native workflow engine** — the original tool XML or Nextflow module is used directly, not translated to a BioLedger spec.

### Search from the CLI

```bash
# Search both sources
bioledger tool search "fastqc"

# Search only the Galaxy Tool Shed
bioledger tool search "bwa" --source toolshed

# Search only nf-core modules
bioledger tool search "samtools" --source nfcore
```

### Run discovered tools in a session

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

**How it works:**
- **Galaxy Tool Shed tools** → downloaded XML is served by planemo (local Galaxy) via `--tool_dir`. A minimal spec is extracted just to generate the `.ga` workflow wrapper.
- **nf-core modules** → downloaded `main.nf` is run directly by Nextflow. No translation to BioLedger spec.

---

## Tool Import: Nextflow → BioLedger Spec

You can still import a Nextflow module as a BioLedger spec (this translates the module into the BioLedger tool spec format for use with any engine):

```bash
bioledger tool import examples/galaxy_tool_import/trimmomatic.nf
# ✓ Imported 'trimmomatic' → ~/.bioledger/tools/trimmomatic.bioledger.yaml
```

BioLedger YAML specs can also be imported directly:

```bash
bioledger tool import specs/samtools_sort.bioledger.yaml
```

> **Note:** Galaxy XML import (`bioledger tool import file.xml`) is no longer supported. Use tool discovery to search and run Galaxy Tool Shed tools directly via planemo.

---

## Tool Export: BioLedger Spec → Galaxy XML or Nextflow

Tool specs are format-agnostic. Export to either platform:

```bash
# To Galaxy XML
bioledger tool export trimmomatic --format galaxy -o trimmomatic.xml

# To Nextflow DSL2
bioledger tool export trimmomatic --format nextflow -o trimmomatic.nf
```

---

## See also

- [Hello World](../hello_bioledger/) — end-to-end walkthrough with ISA-Tab
- [CSV to ISA-Tab](../csv_to_isatab/) — converting samplesheets to structured metadata
- [Variant calling](../variant_calling/) — full pipeline using the public library repos
