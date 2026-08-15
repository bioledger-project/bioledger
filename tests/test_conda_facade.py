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

    def test_submit_spec_raises_not_implemented(self):
        facade = CondaRunnerFacade()
        with pytest.raises(NotImplementedError, match="not yet wired"):
            facade.submit_spec(spec=None, input_mapping={}, params={}, work_dir=Path("/tmp"))
