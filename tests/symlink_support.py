"""Capability-aware creation for tests that require real symbolic links."""

import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"


def create_test_symlink(
    link: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    """Create the real link, skipping only Windows' missing privilege error."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if WINDOWS and getattr(error, "winerror", None) == 1314:
            pytest.skip(
                "Windows symlink creation privilege unavailable (WinError 1314)"
            )
        raise
