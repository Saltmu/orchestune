"""Tests for orchestune.infra.detached_process."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.infra.detached_process import (
    DetachedProcess,
    start_detached_process,
)


class TestDetachedProcessDataClass:
    def test_detached_process_attributes(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        dp = DetachedProcess(pid=1234, log_path=log_file, log_offset=42)
        assert dp.pid == 1234
        assert dp.log_path == log_file
        assert dp.log_offset == 42
        with pytest.raises(dataclasses.FrozenInstanceError):
            dp.pid = 5678  # type: ignore[misc]


class TestStartDetachedProcess:
    def test_start_detached_process_executes_and_merges_streams(
        self, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "agent.log"
        code = (
            "import sys\n"
            "sys.stdout.write('out_line\\n')\n"
            "sys.stderr.write('err_line\\n')\n"
        )
        proc = start_detached_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            log_path=log_path,
        )

        assert proc.pid > 0
        assert proc.log_path == log_path
        assert proc.log_offset == 0

        # Wait for the detached process to finish writing
        # In testing we can wait on the pid via os.waitpid or polling
        import time

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if log_path.exists() and b"err_line" in log_path.read_bytes():
                break
            time.sleep(0.05)

        content = log_path.read_text(encoding="utf-8")
        assert "out_line" in content
        assert "err_line" in content

    def test_start_detached_process_append_mode_records_offset(
        self, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "agent.log"
        initial_data = b"pre-existing log header\n"
        log_path.write_bytes(initial_data)
        initial_len = len(initial_data)

        proc = start_detached_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('second_run\\n')"],
            cwd=tmp_path,
            log_path=log_path,
        )

        assert proc.log_offset == initial_len

        import time

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if b"second_run" in log_path.read_bytes():
                break
            time.sleep(0.05)

        raw = log_path.read_bytes()
        assert raw[:initial_len] == initial_data
        assert b"second_run" in raw[initial_len:]

    def test_start_detached_process_popen_invocation_contract(
        self, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "contract.log"
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = start_detached_process(
                ["my-agent", "--flag"],
                cwd=tmp_path,
                log_path=log_path,
            )

        assert result.pid == 99999
        assert result.log_path == log_path
        assert result.log_offset == 0

        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["start_new_session"] is True
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["stdout"].name == str(log_path)
        # creationflags should not be passed
        assert "creationflags" not in kwargs

    def test_start_detached_process_raises_on_failure(self, tmp_path: Path) -> None:
        log_path = tmp_path / "fail.log"
        with pytest.raises(FileNotFoundError):
            start_detached_process(
                ["non_existent_command_12345"],
                cwd=tmp_path,
                log_path=log_path,
            )
