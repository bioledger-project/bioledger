"""Tests for the _is_process_alive PID liveness helper."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from bioledger.core.engines.protocol import _is_process_alive


class TestIsProcessAlive:
    def test_alive_process_no_start_time(self):
        """A live PID with no expected_start_time should return True."""
        # Use our own PID — it's definitely alive
        assert _is_process_alive(os.getpid()) is True

    def test_dead_process(self):
        """A non-existent PID should return False."""
        # PID 0 is never a valid target for kill in practice on Linux
        # Use a very high PID that's unlikely to exist
        result = _is_process_alive(999999)
        assert result is False

    def test_recycled_pid_detected(self):
        """If psutil reports a different create_time, should return False."""
        pid = os.getpid()
        # Pass a wrong start time — should detect recycling
        with patch("psutil.Process") as mock_process_cls:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = 12345.0  # Wrong time
            mock_process_cls.return_value = mock_proc
            result = _is_process_alive(pid, expected_start_time=99999.0)
            assert result is False

    def test_same_start_time_returns_true(self):
        """If psutil reports the same create_time, should return True."""
        pid = os.getpid()
        with patch("psutil.Process") as mock_process_cls:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = 12345.0
            mock_process_cls.return_value = mock_proc
            result = _is_process_alive(pid, expected_start_time=12345.0)
            assert result is True

    def test_psutil_unavailable_falls_back(self):
        """If psutil import fails, should fall back to os.kill only."""
        pid = os.getpid()
        # Mock the import to fail inside the try block
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            result = _is_process_alive(pid, expected_start_time=12345.0)
            assert result is True  # Falls back to os.kill which says alive

    def test_psutil_no_such_process(self):
        """If psutil.NoSuchProcess is raised, should pass (return True from os.kill)."""
        pid = os.getpid()
        with patch("psutil.Process") as mock_process_cls:
            import psutil

            mock_process_cls.side_effect = psutil.NoSuchProcess(pid)
            result = _is_process_alive(pid, expected_start_time=12345.0)
            assert result is True  # os.kill said alive, psutil exception is caught
