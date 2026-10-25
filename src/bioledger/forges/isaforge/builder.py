from __future__ import annotations

import shutil
from pathlib import Path

from isatools import isatab
from isatools.model import (
    Assay,
    Characteristic,
    DataFile,
    Investigation,
    OntologyAnnotation,
    OntologySource,
    Process,
    Protocol,
    Sample,
    Source,
    Study,
)
from pydantic_ai import RunContext

from bioledger.config import BioLedgerConfig
from bioledger.core.llm.agents import ForgeDeps, make_agent
from bioledger.core.ontology.lookup import search_ontology

from .dataset import ParsedCSV, _infer_format, parse_csv_samplesheet
from .models import ISAStudySpec


# Simple format → likely protocol hints (LLM will confirm/override)
_FORMAT_HINTS: dict[str, str] = {
    "fastq": "nucleotide sequencing",
    "bam": "nucleotide sequencing",
    "sam": "nucleotide sequencing",
    "cram": "nucleotide sequencing",
    "vcf": "sequence variation analysis",
    "bcf": "sequence variation analysis",
    "bed": "genome annotation",
    "gff": "genome annotation",
    "gtf": "genome annotation",
}


def create_investigation(spec: ISAStudySpec) -> Investigation:
    """Build an isatools Investigation from a validated spec."""
    inv = Investigation(identifier=spec.investigation_id)
    inv.title = spec.title
    inv.description = spec.description

    # Ontology sources
    for src in spec.ontology_sources:
        inv.ontology_source_references.append(
            OntologySource(name=src.name, file=src.file, version=src.version)
        )

    study = Study(
        identifier=spec.study_id,
        title=spec.study_title,
        description=spec.study_description,
    )

    # Build sources → samples
    for src_spec in spec.sources:
        source = Source(name=src_spec.name)
        for char in src_spec.characteristics:
            # isatools expects Characteristic(category, value) — a bare
            # OntologyAnnotation silently produces no Characteristics[]
            # column in s_study.txt.
            source.characteristics.append(
                Characteristic(
                    category=OntologyAnnotation(term="Organism"),
                    value=OntologyAnnotation(term=char.term, term_source=None),
                )
            )
        study.sources.append(source)

    for sample_spec in spec.samples:
        sample = Sample(name=sample_spec.name)
        study.samples.append(sample)

    inv.studies.append(study)
    return inv


def write_isatab(investigation: Investigation, output_dir: Path) -> Path:
    """Serialize to ISA-Tab files on disk."""
    output_dir.mkdir(parents=True, exist_ok=True)
    isatab.dump(investigation, str(output_dir))
    return output_dir


async def csv_to_isatab(
    csv_path: Path,
    output_dir: Path,
    config: BioLedgerConfig | None = None,
) -> Path:
    """Convert a CSV samplesheet into a valid ISA-Tab directory with LLM assistance.

    Structural elements (sample names, file names) are inferred programmatically.
    Ontology terms and assay types are determined with LLM assistance via ontology
    lookup. If no config is provided, falls back to basic heuristic inference.

    Args:
        csv_path: Path to the CSV samplesheet.
        output_dir: Directory to write ISA-Tab files into.
        config: BioLedgerConfig for LLM-assisted ontology lookup. If None, uses
            basic heuristics without LLM assistance.

    Returns:
        Path to the output directory containing ISA-Tab files.
    """
    parsed = parse_csv_samplesheet(csv_path)

    # Detect file formats for assay type hints
    detected_formats: set[str] = set()
    for col in parsed.file_columns:
        for row in parsed.rows:
            val = row.get(col, "")
            if val:
                detected_formats.add(_infer_format(val))

    # Use LLM to determine assay type if config available
    if config and detected_formats:
        assay_info = await _llm_assay_inference(
            list(detected_formats), config
        )
        measurement_type = assay_info["measurement_type"]
        technology_type = assay_info["technology_type"]
    else:
        # Fallback: use format hints or default
        fmt = next(iter(detected_formats), None) if detected_formats else None
        hint = _FORMAT_HINTS.get(fmt, "data collection") if fmt else "data collection"
        measurement_type = "sample analysis"
        technology_type = hint

    # Build ISA-Tab structure
    dataset_name = csv_path.stem.replace("_", " ").replace("-", " ").title()

    # Ontology sources
    ncbitaxon = OntologySource(
        name="NCBITaxon", description="NCBI organismal classification"
    )
    obi = OntologySource(
        name="OBI", description="Ontology for Biomedical Investigations"
    )

    # Investigation
    inv = Investigation(identifier=dataset_name.lower().replace(" ", "_"))
    inv.title = dataset_name
    inv.description = f"Generated from CSV samplesheet: {csv_path.name}"
    inv.ontology_source_references = [obi, ncbitaxon]

    # Study
    study = Study(identifier="s_study")
    study.title = dataset_name
    study.description = f"{len(parsed.rows)} samples from {csv_path.name}"
    study.filename = "s_study.txt"

    # Protocols
    # Study-level protocol for source->sample derivation (triggers s_study.txt generation)
    sample_collection_protocol = Protocol(name="sample collection")
    sample_collection_protocol.protocol_type = OntologyAnnotation(
        term="specimen collection", term_source=obi
    )
    # Assay-level protocol for actual experiment
    assay_protocol = Protocol(name=technology_type)
    assay_protocol.protocol_type = OntologyAnnotation(term=technology_type, term_source=obi)
    study.protocols = [sample_collection_protocol, assay_protocol]

    # Build sources and samples with LLM-assisted organism lookup.
    # Resolve each UNIQUE organism string once — a 500-row samplesheet
    # typically has 1-3 distinct organisms, not 500 LLM calls.
    organism_terms: dict[str, str] = {}
    if config and parsed.organism_col:
        org_agent, org_deps = _make_organism_agent(config)
        for row in parsed.rows:
            org_raw = row.get(parsed.organism_col, "").strip()
            if org_raw and org_raw not in organism_terms:
                try:
                    organism_terms[org_raw] = await _llm_organism_lookup(
                        org_raw, org_agent, org_deps
                    )
                except Exception as e:
                    # LLM lookup is best-effort — fall back to the raw
                    # value rather than failing the whole conversion.
                    import logging
                    logging.getLogger(__name__).warning(
                        "Organism lookup failed for %r (%s) — using raw value",
                        org_raw, e,
                    )
                    organism_terms[org_raw] = org_raw

    organism_cat = OntologyAnnotation(term="Organism")
    sources: list[Source] = []
    samples: list[Sample] = []

    # Per-sample data file references for assay
    sample_files: list[tuple[Sample, list[str]]] = []

    for row in parsed.rows:
        sample_name = row.get(parsed.sample_col, "") if parsed.sample_col else ""
        if not sample_name:
            sample_name = f"sample_{len(sources)}"

        # Source with LLM-assisted organism characteristic
        source = Source(name=f"source_{sample_name}")
        if parsed.organism_col:
            org_raw = row.get(parsed.organism_col, "").strip()
            org_term = organism_terms.get(org_raw) or (org_raw or None)

            if org_term:
                char = Characteristic(
                    category=organism_cat,
                    value=OntologyAnnotation(term=org_term, term_source=ncbitaxon),
                )
                source.characteristics = [char]

        sample = Sample(name=sample_name, derives_from=[source])
        sources.append(source)
        samples.append(sample)

        # Collect data file names for this sample
        file_names: list[str] = []
        for col in parsed.file_columns:
            val = row.get(col, "").strip()
            if val:
                file_names.append(val)
        sample_files.append((sample, file_names))

    study.sources = sources
    study.samples = samples

    # Create study-level processes linking sources to samples
    # This is REQUIRED for isatools to generate s_study.txt
    study_processes: list[Process] = []
    for source, sample in zip(sources, samples):
        proc = Process(executes_protocol=sample_collection_protocol)
        proc.inputs = [source]
        proc.outputs = [sample]
        study_processes.append(proc)
    study.process_sequence = study_processes

    # Assay with data files
    assay = Assay(filename="a_assay.txt")
    assay.technology_type = OntologyAnnotation(term=technology_type)
    assay.measurement_type = OntologyAnnotation(term=measurement_type)
    assay.samples = samples

    all_data_files: list[DataFile] = []
    assay_processes: list[Process] = []
    seen_filenames: set[str] = set()

    for sample, file_names in sample_files:
        for fname in file_names:
            df = DataFile(filename=fname, label="Raw Data File")
            if fname not in seen_filenames:
                all_data_files.append(df)
                seen_filenames.add(fname)

            proc = Process(executes_protocol=assay_protocol)
            proc.inputs = [sample]
            proc.outputs = [df]
            assay_processes.append(proc)

    assay.data_files = all_data_files
    assay.process_sequence = assay_processes
    study.assays = [assay]
    inv.studies = [study]

    # Write to disk - isatools generates all ISA-Tab files when process sequences are set
    output_dir.mkdir(parents=True, exist_ok=True)
    isatab.dump(inv, str(output_dir))

    # Copy the original CSV alongside the ISA-Tab files
    shutil.copy2(csv_path, output_dir / csv_path.name)

    return output_dir


def _make_organism_agent(config: BioLedgerConfig):
    """Build the organism-lookup agent + deps once per conversion.

    Returns (agent, deps) so callers can reuse the agent across every
    row's organism instead of rebuilding it per row.
    """
    from pydantic import BaseModel

    from bioledger.ledger.models import LedgerSession

    class OrganismResult(BaseModel):
        organism_label: str
        confidence: str  # "high", "medium", "low"
        reasoning: str

    agent = make_agent(
        config,
        task="ontology_lookup",
        instructions=(
            "You are an ontology expert. Search NCBITaxon for the organism "
            "name provided by the user. Return the exact ontology label that best matches. "
            "Use the search_ontology tool to find the term. If multiple matches, pick "
            "the most common/relevant one for bioinformatics."
        ),
        output_type=OrganismResult,
    )

    @agent.tool
    async def search_ontology_tool(
        ctx: RunContext[ForgeDeps], query: str, ontology: str = "ncbitaxon"
    ) -> str:
        """Search an ontology for terms matching the query."""
        results = await search_ontology(query, ontology=ontology, max_results=5)
        if not results:
            return f"No results for '{query}' in {ontology}"
        return "\n".join(
            f"- {r['label']} ({r['iri']})" for r in results[:5]
        )

    deps = ForgeDeps(session=LedgerSession(), config=config)
    return agent, deps


async def _llm_organism_lookup(organism_raw: str, agent, deps: ForgeDeps) -> str:
    """Use LLM to lookup organism in NCBITaxon ontology.

    Returns the best matching ontology term label.
    """
    result = await agent.run(
        f"Find the NCBITaxon ontology term for: '{organism_raw}'",
        deps=deps,
    )
    return result.output.organism_label


async def _llm_assay_inference(
    file_formats: list[str], config: BioLedgerConfig
) -> dict[str, str]:
    """Use LLM to determine measurement type and technology type from file formats.

    Returns dict with keys: measurement_type, technology_type
    """
    from pydantic import BaseModel

    class AssayTypeResult(BaseModel):
        measurement_type: str
        technology_type: str
        reasoning: str

    agent = make_agent(
        config,
        task="assay_inference",
        instructions=(
            "You are a bioinformatics metadata expert. Given a list of file formats "
            "from a samplesheet, determine the appropriate ISA-Tab measurement type "
            "and technology type. Use OBI ontology terms where possible. "
            "Examples: FASTQ/BAM → 'transcription profiling' + 'nucleotide sequencing'; "
            "VCF → 'genotyping' + 'sequence variation analysis'. "
            "Return the most specific appropriate terms."
        ),
        output_type=AssayTypeResult,
    )

    from bioledger.ledger.models import LedgerSession

    formats_str = ", ".join(file_formats)
    result = await agent.run(
        f"File formats detected: {formats_str}. What are the appropriate "
        f"ISA-Tab measurement type and technology type?",
        deps=ForgeDeps(session=LedgerSession(), config=config),
    )
    return {
        "measurement_type": result.output.measurement_type,
        "technology_type": result.output.technology_type,
    }
