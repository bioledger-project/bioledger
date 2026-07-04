# Variant Calling on *Leishmania donovani*

A practical end-to-end example using BioLedger's **library integration** — no need to bring your own tools or data. This walkthrough downloads a reference genome and paired-end sequencing reads from the public BioLedger libraries, imports bioinformatics tools from the ToolSpec library, and runs a complete variant-calling pipeline: indexing → alignment → sorting → variant calling.

> **Prerequisites**: Docker running, an LLM API key set (see main [README](../../README.md)), and the BioLedger CLI installed with library support: `pip install -e ".[cli,analysis]"`.

> **Scope note**: The steps below use both the **CLI** (`bioledger <command>`) and the **interactive chat session** (`bioledger session new` → `bioledger resume`).
> - **CLI-only** (for now): discovering library contents (`library list`, `study list`), importing tools (`library import`), and downloading remote studies (`study load`).
> - **Chat-supported**: loading local datasets (`load <path>`) and running tools (`run <tool>`). Tools are auto-imported from the library on first use, so explicit `library import` is optional if you only run tools via chat.
>
> Full chat support for `study load` and explicit `library import` is planned but not yet implemented.

---

## 1. Discover what's available

BioLedger hosts two public libraries on GitHub Pages:

- **ToolSpec library** (`toolspec-library`) — curated bioinformatics tool specs
- **ISA-Tab library** (`isatab-library`) — reference genomes and experimental datasets

### Browse the tool library

```bash
bioledger library list
```

Expected output (truncated):

```
NAME                    VERSION   CATEGORIES           CONTAINER
bcftools-filter         1.20      variant-calling      quay.io/biocontainers/samtools:1.20
bcftools-norm           1.20      variant-calling      quay.io/biocontainers/samtools:1.20
bwa-mem2-index          2.2.1     reference-prep       quay.io/biocontainers/bwa-mem2:2.2.1
bwa-mem2-mem            2.2.1     alignment            quay.io/biocontainers/bwa-mem2:2.2.1
gatk-haplotypecaller    4.5.0.0   variant-calling      broadinstitute/gatk:4.5.0.0
samtools-faidx          1.20      reference-prep         quay.io/biocontainers/samtools:1.20
samtools-index          1.20      alignment            quay.io/biocontainers/samtools:1.20
samtools-sort           1.20      alignment            quay.io/biocontainers/samtools:1.20
samtools-dict           1.20      reference-prep         quay.io/biocontainers/samtools:1.20
```

Search for a specific tool:

```bash
bioledger library search bwa
# bwa-mem2-index  v2.2.1   Build FM-index for a reference genome
# bwa-mem2-mem    v2.2.1   Align paired-end reads to an indexed reference
```

### Browse the study library

```bash
bioledger study list
```

Expected output:

```
ACCESSION          ORGANISM                      TYPE             FORMATS
GCF_000002765.6    Plasmodium falciparum 3D7     reference_genome fasta, gff
GCF_000227135.1    Leishmania donovani           reference_genome fasta, gff
PRJNA450813        Leishmania donovani           experimental_data fastq
```

Search for *Leishmania* studies:

```bash
bioledger study search donovani
# GCF_000227135.1  Leishmania donovani  reference_genome  FASTA + GFF
# PRJNA450813      Leishmania donovani  experimental_data 3 paired-end samples
```

Show details for a study:

```bash
bioledger study show GCF_000227135.1
# Organism: Leishmania donovani
# Type: reference_genome
# Files: GCF_000227135.1_ASM22713v2_genomic.fna.gz (fasta), .gff.gz (gff)
# Source: NCBI RefSeq GCF_000227135.1
```

---

## 2. Import the tools you need

Instead of writing tool specs by hand, import them directly from the library:

```bash
# Indexing and reference prep
bioledger library import samtools-faidx
bioledger library import samtools-dict
bioledger library import bwa-mem2-index

# Alignment
bioledger library import bwa-mem2-mem
bioledger library import samtools-sort
bioledger library import samtools-index

# Variant calling
bioledger library import gatk-haplotypecaller
```

Each import fetches the spec YAML from the GitHub repository and saves it locally:

```
~/.bioledger/tools/samtools-faidx.bioledger.yaml
~/.bioledger/tools/samtools-dict.bioledger.yaml
~/.bioledger/tools/bwa-mem2-index.bioledger.yaml
...
```

Verify an import:

```bash
bioledger tool show samtools-faidx
# samtools-faidx  v1.20
#   Container: quay.io/biocontainers/samtools:1.20
#   Inputs:  ref_fasta (fasta, required)
#   Outputs: fai (file)
#   Command: samtools faidx {{inputs.ref_fasta}}
```

---

## 3. Load the reference genome

The *L. donovani* BPK282A1 reference genome (NCBI RefSeq `GCF_000227135.1`) includes a FASTA and a GFF annotation. Download it to the local datasets directory:

```bash
bioledger study load GCF_000227135.1
```

This downloads:

- `~/.bioledger/datasets/GCF_000227135.1/GCF_000227135.1_ASM22713v2_genomic.fna.gz` (~30 MB)
- `~/.bioledger/datasets/GCF_000227135.1/GCF_000227135.1_ASM22713v2_genomic.gff.gz`

The ISA-Tab metadata (`i_investigation.txt`, `s_study.txt`, `manifest.yaml`) is also saved so BioLedger knows the organism, source, and file checksums.

---

## 4. Load the sequencing data

PRJNA450813 contains paired-end Illumina reads from three *L. donovani* clinical isolates (CL, IV, VL — cutaneous, intra-dermal, visceral leishmaniasis). Each sample has ~2 GB of FASTQ data.

```bash
bioledger study load PRJNA450813
```

> **Tip**: The full download is ~12 GB. For a quick test, run with `--no-download` to fetch only the ISA-Tab metadata and manifest, then use a single sample:
>
> ```bash
> bioledger study load PRJNA450813 --no-download
> # ...manually download just SRR7133733_1.fastq.gz and SRR7133733_2.fastq.gz...
> ```

The downloaded data lives at:

```
~/.bioledger/datasets/PRJNA450813/
  i_investigation.txt
  s_study.txt
  a_sequencing.txt
  manifest.yaml
  SRR7133731_1.fastq.gz   # IV sample
  SRR7133731_2.fastq.gz
  SRR7133732_1.fastq.gz   # VL sample
  SRR7133732_2.fastq.gz
  SRR7133733_1.fastq.gz   # CL sample
  SRR7133733_2.fastq.gz
```

---

## 5. Create a session and run the pipeline

Start an interactive session:

```bash
bioledger session new --name "L. donovani variant calling" --description "Calling SNPs across three clinical isolates against BPK282A1 reference"
# Session abc123de created

bioledger resume abc123de
```

### 5.1 Load the reference (once, shared across all runs)

BioLedger keeps track of loaded data as ledger entries:

```
you> load ~/.bioledger/datasets/GCF_000227135.1/

assistant> Loaded reference genome "Leishmania donovani BPK282A1"
            Assembly: GCF_000227135.1
            Chromosomes: 36
            Files: genomic.fna.gz, genomic.gff.gz
```

> **Behind the scenes**: BioLedger records a `data_import` entry with the file paths and SHA-256 hashes. The reference is staged as a copied file in each tool's run directory, so GATK can find its `.fai` and `.dict` sidecars.

### 5.2 Index the reference

NCBI distributes reference genomes as standard gzip (`.gz`), but `samtools faidx` requires
**bgzf** (block gzip) format. Run `bgzip` first to recompress, then run the three indexing steps.
Each gets its own isolated run directory under `~/.bioledger/sessions/abc123de/runs/`:

```
you> run bgzip on GCF_000227135_1_ASM22713v2_genomic.fna.gz
assistant> Suggested: bgzip
           Params: {}
           Run this tool? [y/N]: y
assistant> bgzip completed. Outputs: [GCF_000227135.1_ASM22713v2_genomic.fna.gz]

you> run samtools-faidx on GCF_000227135_1_ASM22713v2_genomic.fna.gz
assistant> Suggested: samtools-faidx
           Params: {}
           Run this tool? [y/N]: y
assistant> samtools-faidx completed. Outputs: [GCF_000227135.1_ASM22713v2_genomic.fna.gz.fai]

you> run samtools-dict on GCF_000227135_1_ASM22713v2_genomic.fna.gz
assistant> samtools-dict completed. Outputs: [GCF_000227135.1_ASM22713v2_genomic.dict]

you> run bwa-mem2-index on GCF_000227135_1_ASM22713v2_genomic.fna.gz
assistant> bwa-mem2-index completed. Outputs: [.0123, .amb, .ann, .bwt.2bit.64, .pac]
```

> **How staging works**: The reference FASTA is an *external* input (outside the session directory), so BioLedger **copies** it into each run directory. When a later step chains from a prior step's output (e.g., the `.fai` from step 1 becomes an input to step 7), BioLedger creates a **relative symlink** so paths resolve correctly inside the container.

### 5.3 Align, sort, and index each sample

We'll process one sample (SRR7133733, the CL isolate). Repeat for the other two samples to call variants across all three.

```
you> run bwa-mem2-mem with ref GCF_000227135_1_ASM22713v2_genomic.fna.gz and reads SRR7133733_1.fastq.gz SRR7133733_2.fastq.gz
assistant> Suggested: bwa-mem2-mem
           Params: {threads: 4, read_group: "@RG\\tID:SRR7133733\\tSM:CL\\tPL:ILLUMINA"}
           Run this tool? [y/N]: y
assistant> bwa-mem2-mem completed. Outputs: [aligned.sam]

you> run samtools-sort on aligned.sam
assistant> samtools-sort completed. Outputs: [sorted.bam]

you> run samtools-index on sorted.bam
assistant> samtools-index completed. Outputs: [sorted.bam.bai]
```

### 5.4 Call variants with GATK

```
you> run gatk-haplotypecaller with ref GCF_000227135_1_ASM22713v2_genomic.fna.gz and bam sorted.bam
assistant> Suggested: gatk-haplotypecaller
           Params: {ploidy: 2, stand_call_conf: 30.0}
           Run this tool? [y/N]: y
assistant> gatk-haplotypecaller completed. Outputs: [output.g.vcf.gz, output.g.vcf.gz.tbi]
```

> **Why it works**: All reference sidecars (`.fai`, `.dict`) and the BAM index (`.bai`) were **colocated** in the same run directory thanks to BioLedger's per-run-directory staging. GATK expects this layout.

### 5.5 Review the ledger

```
you> review

  DATA [a1b2c3] data_import:   GCF_000227135.1 (reference genome)
  TOOL [d4e5f6] tool_run:      samtools-faidx
  TOOL [g7h8i9] tool_run:      samtools-dict
  TOOL [j0k1l2] tool_run:      bwa-mem2-index
  TOOL [m3n4o5] tool_run:      bwa-mem2-mem   parent=d4e5f6
  TOOL [p6q7r8] tool_run:      samtools-sort  parent=m3n4o5
  TOOL [s9t0u1] tool_run:      samtools-index parent=p6q7r8
  TOOL [v2w3x4] tool_run:      gatk-haplotypecaller parent=s9t0u1
```

Each entry links to its parent via `parent_id`, forming a DAG that BioLedger walks during crystallization.

---

## 6. Crystallize the workflow

Convert the ledger into a reproducible Nextflow workflow:

```bash
bioledger crystallize abc123de
```

Output (`workflow.nf`):

```groovy
nextflow.enable.dsl=2

process SAMTOOLS_FAIDX { ... }
process SAMTOOLS_DICT { ... }
process BWA_MEM2_INDEX { ... }
process BWA_MEM2_MEM { ... }
process SAMTOOLS_SORT { ... }
process SAMTOOLS_INDEX { ... }
process GATK_HAPLOTYPECALLER { ... }

workflow {
    // Channels and process calls wired by parent_id
}
```

Or export to Galaxy:

```bash
bioledger crystallize abc123de --format galaxy
```

---

## 7. Package into an RO-Crate

Bundle everything — workflow, data, provenance, and ISA-Tab metadata — into a self-describing archive:

```bash
bioledger package abc123de
# RO-Crate written to ~/.bioledger/crates/abc123de/
#   ro-crate-metadata.json
#   workflow.nf
#   ledger.json
#   GCF_000227135.1_ASM22713v2_genomic.fna.gz
#   output.g.vcf.gz
#   ...
```

The crate includes the full ISA-Tab from the study load step, so anyone consuming the crate knows the organism (*Leishmania donovani*), the reference assembly (GCF_000227135.1), and the sample metadata (CL/IV/VL isolates).

---

## What just happened?

| Step | Command | What BioLedger recorded |
|------|---------|------------------------|
| Discover tools | `bioledger library list` | Fetched 13-spec index from GitHub Pages, cached for 1 hour |
| Discover data | `bioledger study list` | Fetched 3-study index from GitHub Pages |
| Import tools | `bioledger library import <name>` | Fetched spec YAML from `toolspec-library` repo, saved locally |
| Load reference | `bioledger study load GCF_000227135.1` | Downloaded FASTA + GFF + ISA-Tab metadata, recorded `data_import` entry |
| Load reads | `bioledger study load PRJNA450813` | Downloaded FASTQs + ISA-Tab metadata, recorded `data_import` entry |
| Run tools | `run <tool> on <input>` | Each tool run creates an isolated run dir, stages inputs (copy or symlink), executes in Docker, discovers outputs |
| Crystallize | `bioledger crystallize` | Walks the DAG of `parent_id` links to emit a Nextflow/Galaxy workflow |
| Package | `bioledger package` | Bundles workflow, data, ledger, and ISA-Tab into an RO-Crate |

---

## Next steps

- **Run all three samples**: Repeat steps 5.3–5.4 for SRR7133731 (IV) and SRR7133732 (VL), then use `gatk-genotypegvcfs` (also in the library) to jointly genotype all three GVCFs.
- **Add filtering**: Import `bcftools-filter` from the library and apply QUAL/DP filters.
- **Try another organism**: Load the *P. falciparum* reference (`GCF_000002765.6`) and align *P. falciparum* reads against it — the same tools work for any diploid/haploid genome.
