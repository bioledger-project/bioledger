from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from bioledger.core.llm.config import LLMConfig


class BioLedgerConfig(BaseSettings):
    """Global configuration. Loaded from env vars and/or ~/.bioledger/config.yaml."""

    model_config = {
        "env_prefix": "BIOLEDGER_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    home_dir: Path = Field(default_factory=lambda: Path.home() / ".bioledger")
    default_model: str = ""
    llm: LLMConfig = LLMConfig()

    @model_validator(mode="after")
    def _apply_default_model(self) -> "BioLedgerConfig":
        """If BIOLEDGER_DEFAULT_MODEL is set, override llm.default_model
        and all task_models to use the same provider."""
        if self.default_model:
            old_default = self.llm.default_model
            self.llm.default_model = self.default_model
            for task, model in self.llm.task_models.items():
                # Update if exact match on old default OR same provider prefix
                old_provider = model.split(":")[0] if ":" in model else ""
                if model == old_default or old_provider == old_default.split(":")[0]:
                    # Use the new default model for all tasks when switching providers
                    # This ensures model names are valid for the target provider
                    self.llm.task_models[task] = self.default_model
        return self

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        self.home_dir.mkdir(parents=True, exist_ok=True)
        (self.home_dir / "tools").mkdir(exist_ok=True)
        (self.home_dir / "sessions").mkdir(exist_ok=True)
        (self.home_dir / "cache").mkdir(exist_ok=True)
