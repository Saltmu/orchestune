from __future__ import annotations

import tomllib
from pathlib import Path


def test_dispatch_package_has_no_temporary_ble001_per_file_ignore() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    per_file_ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]

    assert not any(
        "dispatch" in pattern and "BLE001" in ignored
        for pattern, ignored in per_file_ignores.items()
    )
