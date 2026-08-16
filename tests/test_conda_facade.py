"""Tests for CondaRunnerFacade — ExecutionAdapter protocol conformance."""

from __future__ import annotations

from pathlib import Path

import pytest

from bioledger.core.engines.conda_facade import CondaRunnerFacade
from bioledger.core.engines.protocol import ExecutionAdapter


class TestCondaFacade:
    def test_conda_facade_is_execution_adapter(self):
        assert isinstance(CondaRunnerFacade(), ExecutionAdapter)

    def test_generate_raises_not_implemented(self):
        facade = CondaRunnerFacade()
        with pytest.raises(NotImplementedError, match="cannot fan-out"):
            facade.generate(spec=None, input_mapping={})

    def test_submit_raises_not_implemented(self):
        facade = CondaRunnerFacade()
        with pytest.raises(NotImplementedError, match="cannot submit generated"):
            facade.submit(handle=None, input_mapping={}, params={}, work_dir=Path("/tmp"))

    def test_submit_spec_is_wired(self):
        """submit_spec should be wired to stage_and_submit (no longer raises)."""
        from unittest.mock import patch, MagicMock

        facade = CondaRunnerFacade()
        mock_toolspec = MagicMock()
        mock_entry = MagicMock()
        mock_entry.id = "test-entry-id"
        mock_entry.run_handle_id = "test-run-id"
        with (
            patch(
                "bioledger.forges.analysisforge.executor.stage_and_submit",
                return_value=(mock_entry, Path("/tmp/run"), None, None, None),
            ),
            patch(
                "bioledger.forges.analysisforge.executor._spec_to_toolspec",
                return_value=mock_toolspec,
            ),
        ):
            result = facade.submit_spec(
                spec=None, input_mapping={}, params={}, work_dir=Path("/tmp")
            )
        assert result.executor == "conda"
        assert result.entry_id == "test-entry-id"
        assert result.run_id == "test-run-id"
