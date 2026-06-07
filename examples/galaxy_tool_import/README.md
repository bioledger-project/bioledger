# Example: Importing a Galaxy Tool

BioLedger's ToolForge can import existing [Galaxy](https://galaxyproject.org/) tool wrappers and convert them into BioLedger tool specs. This means you can reuse the thousands of tools already wrapped for Galaxy without rewriting anything.

## Files in this directory

- **`fastqc.xml`** — Galaxy tool wrapper for [FastQC](https://www.bioinformatics.babraham.ac.uk/projects/fastqc/) from the [IUC repository](https://github.com/galaxyproject/tools-iuc)
- **`trimmomatic.nf`** — Example Nextflow DSL2 process for importing (Trimmomatic)
- **`isatab/`** — Complete ISA-Tab dataset directory with synthetic test data:
  - `i_investigation.txt` — Investigation metadata
  - `s_study.txt` — Study with one sample
  - `a_assay.txt` — Sequencing assay pointing to the FASTQ
  - `sample.fastq` — Tiny synthetic FASTQ file (4 reads)

The Galaxy tool wrapper is the main example:

> **Source:** https://github.com/galaxyproject/tools-iuc/blob/main/tools/fastqc/rgFastQC.xml  
> **Retrieved:** 2025-04-11  
> **Version:** 0.74+galaxy1

This is a real, production-quality tool wrapper (lightly truncated for readability) that demonstrates the full complexity of Galaxy tool definitions including:

- Cheetah templating in the command section
- Conditional logic for different input formats
- Multiple optional parameters
- Bio.tools cross-reference
- Test cases
- Citations

```xml
<tool id="fastqc" name="FastQC" version="0.74+galaxy1">
  <description>Read Quality reports</description>
  <xrefs>
    <xref type="bio.tools">fastqc</xref>
  </xrefs>
  <requirements>
    <requirement type="package" version="0.12.1">fastqc</requirement>
  </requirements>
  <command><![CDATA[
    fastqc
      --outdir '${html_file.files_path}'
      --threads ${GALAXY_SLOTS:-2}
      --quiet
      --extract
      $nogroup
      --kmers $kmers
      '${input_file}'
  ]]></command>
  <inputs>
    <param format="fastq,fastq.gz,fastq.bz2,bam,sam" name="input_file" type="data" />
    <param name="contaminants" type="data" format="tabular" optional="true" />
    <param argument="--adapters" type="data" format="tabular" optional="true" />
    <param name="limits" type="data" format="txt" optional="true" />
    <param argument="--nogroup" type="boolean" truevalue="--nogroup" falsevalue="" />
    <param argument="--min_length" type="integer" optional="true" />
    <param argument="--kmers" type="integer" value="7" min="2" max="10" />
  </inputs>
  <outputs>
    <data format="html" name="html_file" from_work_dir="output.html" />
    <data format="txt" name="text_file" from_work_dir="output.txt" />
  </outputs>
  <tests>
    <test>
      <param name="input_file" value="1000trimmed.fastq" />
      <output name="html_file" file="fastqc_report.html" />
      <output name="text_file" file="fastqc_data.txt" />
    </test>
  </tests>
  <citations>
    <citation type="bibtex">...</citation>
  </citations>
</tool>
```

## Import the tool

```bash
bioledger tool import examples/galaxy_tool_import/fastqc.xml
# ✓ Imported 'fastqc' → ~/.bioledger/tools/fastqc.bioledger.yaml
```

BioLedger parses the Galaxy XML and extracts:

- **Tool metadata** from `<tool>` → id: `fastqc`, name: `FastQC`, version: `0.74+galaxy1`
- **Bio.tools cross-reference** from `<xrefs>` → `fastqc` (for EDAM ontology annotations)
- **Package requirement** from `<requirements>` → `fastqc` v0.12.1 (converted to container)
- **Command template** from `<command>` → Cheetah template with conditional logic
- **Inputs** from `<inputs>` → `input_file` (fastq/fastq.gz/bam/sam)
- **Optional parameters** → `contaminants`, `adapters`, `limits` (data files); `nogroup` (boolean); `min_length`, `kmers` (integers)
- **Outputs** from `<outputs>` → `html_file` (HTML report), `text_file` (raw data)
- **Tests** from `<tests>` → Test cases with input/output file mappings
- **Citations** from `<citations>` → BibTeX reference for Andrews, S.

## Verify the import

```bash
bioledger tool show fastqc
# fastqc  v0.74+galaxy1
#   Source: galaxyproject/tools-iuc (IUC)
#   Package: fastqc v0.12.1 (conda/biocontainers)
#   Inputs:  input_file (fastq/fastq.gz/bam/sam, required)
#   Params:  contaminants, adapters, limits (optional data files)
#            nogroup (boolean flag)
#            min_length (integer, optional)
#            kmers (integer, default=7, min=2, max=10)
#   Outputs: html_file (HTML report), text_file (raw data)

bioledger tool validate ~/.bioledger/tools/fastqc.bioledger.yaml
# ✓ fastqc is valid
```

## LLM-enhanced import (optional)

For complex Galaxy tools with Cheetah templating, use `--use-llm` to get additional analysis and fixes:

```bash
# Requires an LLM API key (OpenAI, Google, Anthropic, or Azure)
export OPENAI_API_KEY="your-key-here"

# Import with LLM assistance
bioledger tool import examples/galaxy_tool_import/fastqc.xml --use-llm

# The LLM will:
# - Check for Galaxy-specific constructs that need manual fixes
# - Identify .element_identifier, .dataset, str() calls, etc.
# - Suggest specific fixes for Cheetah→Jinja2 conversion issues
# - Provide conceptual validation of the tool spec
```

Without `--use-llm`, the import is purely programmatic (faster, no API key needed), but you may need to manually fix Galaxy-specific constructs in the command template.

## Export back to Galaxy or Nextflow

Tool specs are format-agnostic. Export to either platform:

```bash
# Back to Galaxy XML
bioledger tool export fastqc --format galaxy -o fastqc_roundtrip.xml

# To Nextflow DSL2
bioledger tool export fastqc --format nextflow -o fastqc.nf
```

## Import from Nextflow too

ToolForge also imports Nextflow DSL2 processes:

```bash
bioledger tool import examples/galaxy_tool_import/trimmomatic.nf
# ✓ Imported 'trimmomatic' → ~/.bioledger/tools/trimmomatic.bioledger.yaml
```

The `trimmomatic.nf` file in this directory shows a typical Nextflow process with:
- Container image from Biocontainers
- Input tuple with metadata and reads
- Multiple output channels
- Script block with conditional logic

## Test the import in a session

BioLedger runs everything within interactive sessions for provenance tracking. To test FastQC:

```bash
# Import the tool
bioledger tool import examples/galaxy_tool_import/fastqc.xml

# Start a new session
bioledger session new fastqc-test

# Resume the session and load the ISA-Tab dataset
bioledger resume fastqc-test
```

Once in the chat:

```
you> load examples/galaxy_tool_import/isatab/
Loaded dataset "Sample FastQC Test Dataset"
  Samples: 1
  Organisms: Homo sapiens
  File formats: fastq
  Files: 1

you> run fastqc on the sample
assistant> Suggested: fastqc
           Reason: Quality control analysis for the raw FASTQ data
           Run this tool? [y/N]: y
```

The session tracks the complete provenance: dataset loading → tool suggestion → execution → outputs.

## See also

- [Hello World](../hello_bioledger/) — end-to-end walkthrough with ISA-Tab
- [CSV to ISA-Tab](../csv_to_isatab/) — converting samplesheets to structured metadata
