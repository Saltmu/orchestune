"""Run a CLI with its stdout faked as an interactive terminal (#1409)."""

from __future__ import annotations

import io
import sys

import pytest


class FakeTtyStream(io.StringIO):
    """A text stream that reports itself as a UTF-8 TTY."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


def patch_tty_stdout(monkeypatch: pytest.MonkeyPatch) -> FakeTtyStream:
    """Route ``sys.stdout`` to a fake TTY with the Rich-enabling environment."""
    for name in ("ORCHESTUNE_CLI_PLAIN", "CI", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "200")
    stream = FakeTtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    return stream
