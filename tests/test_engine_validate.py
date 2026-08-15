"""Tests for K3: engine config validation."""

from __future__ import annotations

from unittest.mock import MagicMock

from bioledger.core.engines.config import (
    EnginesConfig,
    GalaxyEngineConfig,
    NextflowEngineConfig,
    SnakemakeEngineConfig,
)
from bioledger.core.engines.validate import (
    format_warnings,
    validate_engine_config,
)


class TestValidateEngineConfig:
    def test_clean_config_no_warnings(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="apptainer"),
            galaxy=GalaxyEngineConfig(
                mode="remote",
                url="https://usegalaxy.org",
                api_key="key",
            ),
        )
        result = validate_engine_config(config)
        assert result.ok

    def test_snakemake_none_runtime_warns(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="none"),
        )
        result = validate_engine_config(config)
        assert not result.ok
        smk_warnings = [w for w in result if w.engine == "snakemake"]
        assert any("container_runtime is 'none'" in w.message for w in smk_warnings)

    def test_snakemake_cluster_with_jobs_one_warns(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(
                jobs=1,
                extra_args=["--cluster", "sbatch --partition=normal"],
            ),
        )
        result = validate_engine_config(config)
        smk_warnings = [w for w in result if w.engine == "snakemake"]
        assert any("jobs=1" in w.message for w in smk_warnings)

    def test_snakemake_cluster_with_jobs_ok(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(
                jobs=8,
                extra_args=["--cluster", "sbatch --partition=normal"],
            ),
        )
        result = validate_engine_config(config)
        smk_warnings = [
            w for w in result if w.engine == "snakemake"
            and "jobs=1" in w.message
        ]
        assert len(smk_warnings) == 0

    def test_galaxy_planemo_warns(self):
        config = EnginesConfig(
            galaxy=GalaxyEngineConfig(mode="planemo"),
        )
        result = validate_engine_config(config)
        gxy_warnings = [w for w in result if w.engine == "galaxy"]
        assert any("planemo" in w.message for w in gxy_warnings)

    def test_galaxy_remote_no_warning(self):
        config = EnginesConfig(
            galaxy=GalaxyEngineConfig(
                mode="remote",
                url="https://usegalaxy.org",
                api_key="secret",
            ),
        )
        result = validate_engine_config(config)
        gxy_warnings = [
            w for w in result if w.engine == "galaxy"
            and "planemo" in w.message
        ]
        assert len(gxy_warnings) == 0

    def test_missing_executable_warns(self, tmp_path):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(executable="/nonexistent/snakemake"),
        )
        result = validate_engine_config(config)
        smk_warnings = [w for w in result if w.engine == "snakemake"]
        assert any("not found" in w.message for w in smk_warnings)

    def test_missing_config_file_warns(self, tmp_path):
        config = EnginesConfig(
            nextflow=NextflowEngineConfig(
                config_file=tmp_path / "nonexistent.config",
            ),
        )
        result = validate_engine_config(config)
        nf_warnings = [w for w in result if w.engine == "nextflow"]
        assert any("does not exist" in w.message for w in nf_warnings)

    def test_existing_config_file_no_warning(self, tmp_path):
        cfg_file = tmp_path / "nextflow.config"
        cfg_file.write_text("docker { enabled = true }")
        config = EnginesConfig(
            nextflow=NextflowEngineConfig(config_file=cfg_file),
        )
        result = validate_engine_config(config)
        nf_warnings = [
            w for w in result if w.engine == "nextflow"
            and "does not exist" in w.message
        ]
        assert len(nf_warnings) == 0


class TestContainerMismatch:
    """K3: Container tech mismatch detection with specs."""

    def _make_spec(self, container="", conda_deps=None):
        spec = MagicMock()
        spec.container = container if container else None
        spec.conda_deps = conda_deps
        return spec

    def test_container_specs_with_snakemake_none_warns(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="none"),
        )
        specs = [self._make_spec(container="quay.io/biocontainers/test:1.0")]
        result = validate_engine_config(config, specs)
        smk_warnings = [
            w for w in result if w.engine == "snakemake"
            and "container_runtime='none'" in w.message
        ]
        assert len(smk_warnings) == 1

    def test_container_specs_with_snakemake_apptainer_ok(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="apptainer"),
        )
        specs = [self._make_spec(container="quay.io/biocontainers/test:1.0")]
        result = validate_engine_config(config, specs)
        mismatch_warnings = [
            w for w in result if "container_runtime='none'" in w.message
        ]
        assert len(mismatch_warnings) == 0

    def test_conda_specs_with_snakemake_apptainer_warns(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="apptainer"),
        )
        specs = [self._make_spec(conda_deps=["samtools=1.17"])]
        result = validate_engine_config(config, specs)
        smk_warnings = [
            w for w in result if w.engine == "snakemake"
            and "conda_deps" in w.message
        ]
        assert len(smk_warnings) == 1

    def test_conda_specs_with_snakemake_conda_ok(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="conda"),
        )
        specs = [self._make_spec(conda_deps=["samtools=1.17"])]
        result = validate_engine_config(config, specs)
        conda_mismatch = [
            w for w in result if "conda_deps" in w.message
        ]
        assert len(conda_mismatch) == 0

    def test_container_specs_with_nextflow_no_config_warns(self):
        config = EnginesConfig()
        specs = [self._make_spec(container="quay.io/biocontainers/test:1.0")]
        result = validate_engine_config(config, specs)
        nf_warnings = [
            w for w in result if w.engine == "nextflow"
            and "containers may not be enabled" in w.message
        ]
        assert len(nf_warnings) == 1

    def test_container_specs_with_nextflow_profile_ok(self):
        config = EnginesConfig(
            nextflow=NextflowEngineConfig(
                extra_args=["-profile", "docker"],
            ),
        )
        specs = [self._make_spec(container="quay.io/biocontainers/test:1.0")]
        result = validate_engine_config(config, specs)
        nf_mismatch = [
            w for w in result
            if "containers may not be enabled" in w.message
        ]
        assert len(nf_mismatch) == 0

    def test_no_specs_no_mismatch_warnings(self):
        config = EnginesConfig()
        result = validate_engine_config(config)
        # Only planemo warning expected, no mismatch warnings
        mismatch_warnings = [
            w for w in result
            if "container" in w.message.lower()
            and "container_runtime is 'none'" not in w.message
        ]
        assert len(mismatch_warnings) == 0


class TestFormatWarnings:
    def test_ok(self):
        result = validate_engine_config(EnginesConfig(
            galaxy=GalaxyEngineConfig(
                mode="remote",
                url="https://usegalaxy.org",
                api_key="key",
            ),
        ))
        text = format_warnings(result)
        assert "OK" in text

    def test_with_warnings(self):
        config = EnginesConfig(
            snakemake=SnakemakeEngineConfig(container_runtime="none"),
        )
        result = validate_engine_config(config)
        text = format_warnings(result)
        assert "warning" in text.lower()
        assert "snakemake" in text
        assert "container_runtime" in text
