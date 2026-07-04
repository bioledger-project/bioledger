# Example: CSV Samplesheet to ISA-Tab

> 🚧 **Work in progress** — automatic CSV-to-ISA-Tab conversion is still under construction. The workflow below describes the intended behavior, not a tested one yet.

Most researchers start with a CSV samplesheet, not ISA-Tab. BioLedger's AnalysisForge automatically routes CSVs through ISAForge to generate proper ISA-Tab metadata before loading. This ensures strong provenance tracking even when you don't have ISA-Tab yet.

## Why ISA-Tab matters

ISA-Tab gives BioLedger structured metadata that a plain CSV can't:

| Feature | CSV | ISA-Tab |
|---------|-----|---------|
| Organism annotations | Free text column | Ontology-backed terms (NCBITAXON) |
| Assay type | Not captured | Explicit (e.g., "nucleotide sequencing") |
| Source → Sample lineage | Implicit | Explicit graph |
| File → Sample linkage | Column convention | Formal assay table |
| Ontology references | None | OBI, EFO, etc. |

The RO-Crate you produce at the end is self-describing when backed by ISA-Tab.

## Starting point

This directory contains a typical samplesheet:

```csv
sample_id,organism,reads_file
S001,Mus musculus,S001_R1.fastq.gz
S002,Mus musculus,S002_R1.fastq.gz
S003,Homo sapiens,S003_R1.fastq.gz
```

## How BioLedger handles CSV

When you `load` a CSV in AnalysisForge, it automatically:

1. **Passes through ISAForge** — detects sample IDs, organisms, and data file references
2. **Builds ISA-Tab** — generates investigation, study, and assay files with proper ontology terms
3. **Infers assay type** — FASTQ files → "nucleotide sequencing", BAM → "transcription profiling"
4. **Loads the ISA-Tab** — proceeds with full structured metadata

```bash
bioledger session new --name "csv demo"
bioledger resume <session_id>
```

```
you> load examples/csv_to_isatab/samples.csv

Loaded dataset "Samples" (converted to ISA-Tab at ~/.bioledger/datasets/Samples/)
  Samples: 3
  Organisms: Mus musculus, Homo sapiens
  Assay type: nucleotide sequencing
  File formats: fastq
  Files: 3
```

Behind the scenes, BioLedger created:

```
~/.bioledger/datasets/Samples/
  i_investigation.txt     # Investigation metadata
  s_study.txt            # Sources → Samples with organism annotations
  a_assay.txt            # Samples → Data files via sequencing protocol
  samples.csv            # Original CSV (preserved)
```

## Architecture: ISAForge vs AnalysisForge

- **ISAForge** — ingests and validates samplesheets, builds ISA-Tab, handles metadata operations
- **AnalysisForge** — only works with ISA-Tab internally; CSVs are routed through ISAForge first

This separation keeps AnalysisForge's provenance tracking strong while supporting the common case of starting with a CSV.

## See also

- [Hello World](../hello_bioledger/) — end-to-end walkthrough starting from ISA-Tab
- [Galaxy tool import](../galaxy_tool_import/) — importing tool specs from Galaxy XML
