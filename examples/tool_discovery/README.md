# Example: Tool Discovery

BioLedger can discover tools from external repositories — the [Galaxy Tool Shed](https://toolshed.g2.bx.psu.edu/) and [nf-core modules](https://nf-co.re/modules) — and run them with their native workflow engines. No translation to BioLedger specs is required.

## Files in this directory

- **`trimmomatic.nf`** — Example Nextflow DSL2 module (kept as reference; not importable as a BioLedger spec)
- **`isatab/`** — Complete ISA-Tab dataset directory with synthetic test data:
  - `i_investigation.txt` — Investigation metadata
  - `s_study.txt` — Study with one sample
  - `a_assay.txt` — Sequencing assay pointing to the FASTQ
  - `sample.fastq` — Tiny synthetic FASTQ file (4 reads)

---

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

---

## Run discovered tools in a session

Inside an interactive session, the agent can search external repositories and run discovered tools directly:

```bash
bioledger session new tool-discovery-test
bioledger resume tool-discovery-test
```

```
you> load examples/tool_discovery/isatab/
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

## Import a BioLedger YAML spec

If you have a BioLedger tool spec (`.bioledger.yaml`), you can import it to the local store:

```bash
bioledger tool import specs/samtools_sort.bioledger.yaml
```

---

## See also

- [Hello World](../hello_bioledger/) — end-to-end walkthrough with ISA-Tab
- [CSV to ISA-Tab](../csv_to_isatab/) — converting samplesheets to structured metadata
- [Variant calling](../variant_calling/) — full pipeline using the public library repos
