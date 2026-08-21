from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "detect_bloat.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "detect_bloat_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        """
[tool.detect-bloat]
code_paths = ["orchestune"]
test_paths = ["tests"]
skills_path = "skills"
code_file_lines = 100
test_file_lines = 100
skill_directory_lines = 50
function_lines = 10
""".lstrip(),
        encoding="utf-8",
    )


def test_large_function_is_reported_even_when_its_file_is_small(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    source_dir.joinpath("small.py").write_text(
        "def too_long():\n" + "    pass\n" * 11,
        encoding="utf-8",
    )

    reports = detect_bloat.scan_project(tmp_path)

    assert [(report.category, report.symbol, report.lines) for report in reports] == [
        ("function", "too_long", 12)
    ]


def test_skill_directory_counts_all_markdown_including_references(
    tmp_path: Path,
) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    reference_dir = tmp_path / "skills" / "sample" / "references"
    reference_dir.mkdir(parents=True)
    reference_dir.parent.joinpath("SKILL.md").write_text(
        "base\n" * 30, encoding="utf-8"
    )
    reference_dir.joinpath("details.md").write_text("detail\n" * 21, encoding="utf-8")

    reports = detect_bloat.scan_project(tmp_path)

    report_summary = [
        (report.category, report.path.name, report.lines) for report in reports
    ]
    assert report_summary == [("skill", "sample", 51)]


def test_tests_are_reported_as_warnings(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("test_large.py").write_text("pass\n" * 101, encoding="utf-8")

    reports = detect_bloat.scan_project(tmp_path)

    report_summary = [
        (report.category, report.path.name, report.lines) for report in reports
    ]
    assert report_summary == [("test", "test_large.py", 101)]


def test_warn_only_returns_zero_when_bloat_is_detected(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    source_dir.joinpath("large.py").write_text("pass\n" * 101, encoding="utf-8")

    assert detect_bloat.main(["--root", str(tmp_path), "--warn-only"]) == 0
    assert detect_bloat.main(["--root", str(tmp_path)]) == 1
