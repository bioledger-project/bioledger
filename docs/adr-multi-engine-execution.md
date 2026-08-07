# ADR: Multi-Engine Execution Strategy

**Status**: Accepted
**Date**: 2025-08-06
**Supersedes**: None

## Context

BioLedger originally supported only Docker-based single-tool execution. The
multi-engine strategy adds support for workflow engines (Snakemake, Nextflow,
Galaxy) to enable fan-out (scatter) operations, native scatter for workflow
toolspecs, and engine-native provenance — while keeping the existing Docker
single-tool path as the default for most runs.

## Decisions

### 1. Orchestrate-only scope

BioLedger orchestrates tool execution via engines; it does not reimplement
scheduling, container management, or provenance collection. Engines manage
their own internal tasks; BioLedger records one `LedgerEntry` per submission.

### 2. No hard dependency on any engine

Engine adapters are optional. `bioledger[snakemake]`, `bioledger[galaxy]`,
and `bioledger[nextflow]` are pip extras. The core package works with Docker
only. `dispatch()` auto-detects installed engines and errors with install
instructions when none are available for a fan-out request.

### 3. Passthrough-only config

`EnginesConfig` and per-engine submodels (`NextflowEngineConfig`,
`SnakemakeEngineConfig`, `GalaxyEngineConfig`) have only passthrough fields
(`executable`, `config_file`, `extra_args`). No cross-engine abstract fields.
Galaxy is the only engine with additional fields (`mode`, `url`, `api_key`)
because remote mode requires connection parameters.

### 4. Two dispatch paths

- **Single-item dispatch** (`submit_spec()`): Run one tool from its toolspec.
  Works for Docker/conda toolspecs (via Docker facade) and workflow toolspecs
  with engine executors (native scatter). No `generate()` call — the toolspec
  IS the spec.
- **Fan-out** (`generate()` + `submit()`): Emit a parameterized scatter
  workflow from a docker/conda toolspec + input collection, then bind actual
  values and launch. Container image is baked into the generated text at
  compile time, not passed as a runtime override.

### 5. Galaxy tool-availability check

Galaxy adapter errors if a tool is not available on the target Galaxy
instance. No Docker fallback — Galaxy's tool registry is the source of truth.

### 6. Toolspec executor enum

`ExecutorType = Literal["docker", "conda", "nextflow", "snakemake", "galaxy"]`
on `ExecutionSpec.executors`. Workflows are toolspecs too — a workflow
toolspec has `executors: ["nextflow"]` (or snakemake/galaxy). At most one
engine executor per toolspec (command syntax is engine-specific).
`docker`/`conda` may coexist with each other and with one engine executor.

### 7. `conda_deps` preferred over `container`

`conda_deps: list[str]` on `ExecutionSpec` is mutually exclusive with
`container`. When `conda_deps` is set, the container image is auto-derived
via mulled-hash/biocontainers lookup (pre-Phase-0 mechanism). This makes
toolspecs portable without hardcoding container URIs.

### 8. `ToolInput.collection` for scatter-eligible inputs

`ToolInput.collection: bool = False` marks inputs that accept collections
(globs, manifests, ledger-sourced file lists) for scatter. `generate()` uses
this to determine which inputs become scatter channels. `dispatch()` rejects
collection values for non-collection inputs to prevent accidental fan-out.

### 9. One-entry-per-fan-out provenance model

Fan-out produces one `LedgerEntry` per submission (not one per scattered
task). The engine manages its own internal tasks; the ledger records the
submission with aggregate stats. Engine-native provenance (NF trace, snakemake
stats, Galaxy invocations) is referenced as a `FileRef(role="log")` for
per-task detail. This is consistent with how workflow toolspecs are handled.

### 10. `container_id` → `run_handle_id` rename

`LedgerEntry.container_id` renamed to `run_handle_id` — a generic async-run
identifier that holds either a Docker container ID or an engine-native run ID
(NF run name, snakemake job ID, Galaxy invocation ID). Greenfield — no
existing stored entries to migrate.

### 11. Phase 4 RO-Crate `mainEntity` decision

RO-Crate packaging uses a composite top-level workflow as `mainEntity`. The
single fan-out entry is the workflow-level entity, with engine-native
provenance referenced as a log.

## Consequences

- The `ExecutionAdapter` protocol is the single interface for all execution
  backends, including the Docker facade (which raises `NotImplementedError`
  for `generate()`/`submit()`).
- `dispatch()` is the single entry point for tool execution — no direct
  adapter construction outside of it.
- The ledger model is backward-compatible (pydantic v2 `extra="ignore"`),
  but the `container_id` → `run_handle_id` rename is a breaking change to
  the field name (accepted as greenfield).
- Engine adapters are registered via `register_adapter()` and looked up by
  engine name in `dispatch()`.
