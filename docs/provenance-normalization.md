# Provenance Normalization Specification

## Purpose

Defines the canonical mapping from engine-native provenance artifacts to a
common shape that RO-Crate packaging and the ledger can consume uniformly.

Each engine produces provenance in its own format:
- **Nextflow**: `trace.txt` (TSV) + `timeline.html` + `.nextflow.log`
- **Snakemake**: `stats.json` (or `snakemake.log`) + per-rule logs
- **Galaxy**: `invocation.json` + per-tool `job.json` (via bioblend API)

The normalized shape is a list of `NormalizedStep` records:

```python
class NormalizedStep(BaseModel):
    step_name: str          # process/rule/tool name
    tool_identity: str      # tool name + version (e.g. "samtools:1.17")
    container: str          # container image used (or "" if conda/native)
    command: str            # executed command (best-effort)
    inputs: list[FileRef]   # input files with paths
    outputs: list[FileRef]  # output files with paths
    exit_status: int        # 0=success, non-zero=failure
    duration_seconds: float # wall-clock time
    logs: list[Path]        # log file paths
```

## Nextflow trace.txt Mapping

Nextflow's `-with-trace` produces a TSV with columns:
`task_id`, `process`, `tag`, `hash`, `native_id`, `attempt`, `complete`, `exit`, `duration`, `realtime`, `%cpu`, `%mem`, `vmem`, `rss`, `peak_rss`, `peak_vmem`, `rchar`, `wchar`, `scratch`, `work_dir`, `name`.

Mapping:
| trace.txt field | NormalizedStep field |
|---|---|
| `process` | `step_name` |
| `name` (or `tag`) | `tool_identity` (parsed: tool name from process name) |
| (from process config) | `container` (baked into process directive) |
| (from work_dir command) | `command` (`.command.sh` in work_dir) |
| (from work_dir inputs) | `inputs` (symlinks in work_dir) |
| (from work_dir outputs) | `outputs` (files matching output globs) |
| `exit` | `exit_status` |
| `duration` or `realtime` | `duration_seconds` (parse "1m 30s" format) |
| `.nextflow.log` | `logs` |

See `tests/fixtures/provenance/nextflow_trace.txt` for a sample.

## Snakemake stats Mapping

Snakemake's `--stats stats.json` produces a JSON with per-rule stats.

Mapping:
| stats.json field | NormalizedStep field |
|---|---|
| rule name (top-level keys) | `step_name` |
| rule name | `tool_identity` |
| (from rule's container directive) | `container` |
| (from rule's shell/benchmark) | `command` |
| (from rule's input) | `inputs` |
| (from rule's output) | `outputs` |
| (from job exit code) | `exit_status` |
| `mean` (under `runtime` or `total_runtime`) | `duration_seconds` |
| `snakemake.log` | `logs` |

See `tests/fixtures/provenance/snakemake_stats.json` for a sample.

## Galaxy Invocation Mapping

Galaxy's invocation JSON (via bioblend `invocations.show_invocation`)
contains workflow invocation metadata with per-step job info.

Mapping:
| invocation.json field | NormalizedStep field |
|---|---|
| `steps[].tool_id` | `step_name` |
| `steps[].tool_id` + `tool_version` | `tool_identity` |
| (from job `tool_id` → tool config) | `container` |
| (from job command_line) | `command` |
| (from job input datasets) | `inputs` |
| (from job output datasets) | `outputs` |
| `steps[].job_state` → exit mapping | `exit_status` (ok→0, error→1) |
| `steps[].update_time - create_time` | `duration_seconds` |
| (from job stdout/stderr files) | `logs` |

See `tests/fixtures/provenance/galaxy_invocation.json` for a sample.

## Validation

The mapping is validated against the committed fixture files in
`tests/fixtures/provenance/`. Each fixture is a real (or realistic) sample
of the engine's provenance output. The normalization tests parse each
fixture and verify the mapping produces the expected `NormalizedStep` list.
