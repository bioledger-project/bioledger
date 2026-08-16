# Cluster Execution Guide

BioLedger supports running tools on HPC clusters through each engine's native
cluster integration. BioLedger itself does not implement scheduler plugins —
instead, it passes through to the engine's own cluster config.

## Quick start

1. **Validate your config**: `bioledger config validate`
2. **Pick an example** from `examples/cluster/` and customize it
3. **Resume a session**: `bioledger resume <session>` (uses your engine config)

---

## Nextflow on Slurm with Singularity

Nextflow has first-class Slurm support. Set `engines.nextflow.config_file` to
a `nextflow.config` that enables Slurm and Singularity:

```nextflow
// cluster-nextflow.config
process.executor = 'slurm'
process.queue = 'normal'
process.time = '12h'
process.cpus = 4

singularity {
    enabled = true
    runOptions = '--cleanenv'
}
```

BioLedger config:

```yaml
engines:
  default: nextflow
  nextflow:
    config_file: /path/to/cluster-nextflow.config
```

Or use profiles instead of a config file:

```yaml
engines:
  nextflow:
    extra_args:
      - "-profile"
      - "slurm,singularity"
```

**Crystallized workflows**: The generated `nextflow.config` in RO-Crates
includes commented-out Slurm and Singularity profile blocks. Uncomment them
or use `-profile slurm` when re-running a crystallized workflow.

See: `examples/cluster/bioledger-slurm-nextflow.yaml`

---

## Snakemake on Slurm

Snakemake submits jobs via `--cluster "sbatch ..."` or its own profile system.

BioLedger config:

```yaml
engines:
  default: snakemake
  snakemake:
    container_runtime: singularity  # or apptainer
    jobs: 8                          # parallel jobs to submit
    extra_args:
      - "--cluster"
      - "sbatch --partition=normal --time=12:00:00 --cpus-per-task=4"
```

Key settings:
- **`container_runtime`**: `apptainer`, `singularity`, `conda`, or `none`
- **`jobs`**: Number of parallel jobs (passed as `--jobs`)
- **`extra_args`**: Raw CLI flags — use `--cluster` for sbatch, or `--profile`
  for Snakemake's profile system

See: `examples/cluster/bioledger-slurm-snakemake.yaml`

---

## Galaxy (remote server)

For cluster use, Galaxy must be configured as `mode: "remote"`. The Galaxy
server handles scheduler submission via its own job config — BioLedger just
submits workflows through the API.

```yaml
engines:
  default: galaxy
  galaxy:
    mode: remote
    url: https://galaxy.example.org
    api_key: your-api-key-here
```

Get your API key from: User → Preferences → Manage API Key in the Galaxy UI.
Or set it via the `BIOLEDGER_GALAXY_API_KEY` environment variable.

**Note**: `mode: "planemo"` is for local development only. The config
validator will warn about this.

**Fan-out/scatter**: Galaxy fan-out is not yet supported. Fan-out (running
a tool across a collection of files) requires Snakemake or Nextflow. Galaxy
is supported for single-tool runs and Tool Shed discovery.

See: `examples/cluster/bioledger-galaxy-remote.yaml`

---

## Config validation

Run `bioledger config validate` to check for common mistakes:

- Engine `executable` set but not found on PATH
- Engine `config_file` set but doesn't exist
- Snakemake `container_runtime = "none"` with containerized tools
- Snakemake `--cluster` in `extra_args` but `jobs = 1`
- Galaxy `mode = "planemo"` (local development only)
- Nextflow specs with containers but no config_file or `-profile`

Validation produces **warnings**, not errors — the config is always usable,
but may produce confusing runtime failures if warnings are ignored.

---

## Crystallized workflows on clusters

When you crystallize a session into an RO-Crate (`bioledger crystallize`), the
generated `nextflow.config` includes profile blocks for all container runtimes:

```nextflow
profiles {
    docker { docker.enabled = true }
    singularity { singularity.enabled = true }
    conda { conda.enabled = true }
    // Slurm cluster — uncomment and customize
    // slurm {
    //     process.executor = 'slurm'
    //     process.queue = 'normal'
    // }
}
```

To run a crystallized workflow on a cluster:

```bash
nextflow run workflow.nf -profile singularity,slurm
```

The profile matching the original execution is uncommented by default.
Switch profiles with `-profile` as needed.
