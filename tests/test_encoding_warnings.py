"""Check Python's default-encoding warning in an isolated UTF-8 process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_IO_OPERATIONS = (
    (
        "open",
        'open(path, "w").write("text")',
        'open(path, "w", encoding="utf-8").write("text")',
    ),
    ("read_text", "path.read_text()", 'path.read_text(encoding="utf-8")'),
    (
        "write_text",
        'path.write_text("text")',
        'path.write_text("text", encoding="utf-8")',
    ),
)


@pytest.mark.skipif(sys.platform != "linux", reason="requires a UTF-8 Linux locale")
@pytest.mark.parametrize(
    ("operation", "implicit_source", "explicit_source"), _IO_OPERATIONS
)
def test_default_encoding_warning_is_controlled_by_explicit_utf8(
    tmp_path: Path, operation: str, implicit_source: str, explicit_source: str
) -> None:
    path = tmp_path / "text.txt"
    path.write_text("text", encoding="utf-8")
    source = implicit_source
    code = (
        "import locale, sys\n"
        "from pathlib import Path\n"
        "assert locale.getencoding().lower().replace('-', '') == 'utf8'\n"
        "path = Path(sys.argv[1])\n"
        f"{source}\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONWARNDEFAULTENCODING": "1",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }
    )
    env.pop("PYTHONUTF8", None)

    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "EncodingWarning" in result.stderr, (operation, result.stderr)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code.replace(implicit_source, explicit_source),
            str(path),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EncodingWarning" not in result.stderr, (operation, result.stderr)


@pytest.mark.skipif(
    os.environ.get("PYTHONWARNDEFAULTENCODING") != "1"
    or "PYTEST_XDIST_WORKER" not in os.environ,
    reason="the local CI warning setting must be active in an xdist worker",
)
def test_xdist_worker_passes_encoding_warning_setting_to_python_children() -> None:
    assert os.environ["PYTHONWARNDEFAULTENCODING"] == "1"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['PYTHONWARNDEFAULTENCODING'])",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "1"
