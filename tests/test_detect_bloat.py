from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

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


def test_warn_only_tolerates_non_utf8_files(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    source_dir.joinpath("invalid.py").write_bytes(b"\xff")

    assert detect_bloat.main(["--root", str(tmp_path), "--warn-only"]) == 0


def test_recorded_baseline_allows_unchanged_warnings(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    source_dir.joinpath("large.py").write_text("pass\n" * 101, encoding="utf-8")
    baseline = tmp_path / "bloat-baseline.json"

    assert (
        detect_bloat.main(["--root", str(tmp_path), "--record-baseline", str(baseline)])
        == 0
    )
    assert (
        detect_bloat.main(["--root", str(tmp_path), "--baseline", str(baseline)]) == 0
    )

    snapshot = json.loads(baseline.read_text(encoding="utf-8"))
    assert snapshot == {
        "version": 1,
        "warnings": [
            {
                "category": "code",
                "limit": 100,
                "lines": 101,
                "path": "orchestune/large.py",
                "symbol": None,
            }
        ],
    }


def test_baseline_comparison_fails_for_new_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    baseline = tmp_path / "bloat-baseline.json"
    assert (
        detect_bloat.main(["--root", str(tmp_path), "--record-baseline", str(baseline)])
        == 0
    )
    source_dir.joinpath("large.py").write_text("pass\n" * 101, encoding="utf-8")

    assert (
        detect_bloat.main(["--root", str(tmp_path), "--baseline", str(baseline)]) == 1
    )
    assert (
        "[new] [code] orchestune/large.py has 101 lines (limit: 100)"
        in capsys.readouterr().out
    )


def test_baseline_comparison_fails_when_warning_grows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    source_dir = tmp_path / "orchestune"
    source_dir.mkdir()
    source = source_dir / "large.py"
    source.write_text("pass\n" * 101, encoding="utf-8")
    baseline = tmp_path / "bloat-baseline.json"
    assert (
        detect_bloat.main(["--root", str(tmp_path), "--record-baseline", str(baseline)])
        == 0
    )
    source.write_text("pass\n" * 102, encoding="utf-8")

    assert (
        detect_bloat.main(["--root", str(tmp_path), "--baseline", str(baseline)]) == 1
    )
    assert (
        "[worsened] [code] orchestune/large.py has 102 lines (was 101; limit: 100)"
        in capsys.readouterr().out
    )


def test_record_baseline_creates_parent_directory(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    _write_config(tmp_path)
    baseline = tmp_path / ".orchestune" / "bloat-baseline.json"

    assert (
        detect_bloat.main(["--root", str(tmp_path), "--record-baseline", str(baseline)])
        == 0
    )
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "version": 1,
        "warnings": [],
    }


def test_duplicate_warning_identities_are_compared_individually(tmp_path: Path) -> None:
    detect_bloat = _load_script()
    source = tmp_path / "orchestune" / "conditional.py"
    source.parent.mkdir()
    baseline_reports = [
        detect_bloat.BloatReport("function", source, 11, 10, "too_long"),
        detect_bloat.BloatReport("function", source, 12, 10, "too_long"),
    ]
    baseline = tmp_path / "bloat-baseline.json"
    detect_bloat.write_baseline(baseline, baseline_reports, tmp_path)

    regressions = detect_bloat.find_regressions(
        [
            detect_bloat.BloatReport("function", source, 11, 10, "too_long"),
            detect_bloat.BloatReport("function", source, 13, 10, "too_long"),
        ],
        detect_bloat.load_baseline(baseline),
        tmp_path,
    )

    assert [(item.report.lines, item.previous_lines) for item in regressions] == [
        (13, 12)
    ]


def test_duplicate_warning_comparison_is_independent_of_report_order(
    tmp_path: Path,
) -> None:
    detect_bloat = _load_script()
    source = tmp_path / "orchestune" / "conditional.py"
    source.parent.mkdir()
    baseline = tmp_path / "bloat-baseline.json"
    detect_bloat.write_baseline(
        baseline,
        [
            detect_bloat.BloatReport("function", source, 5, 4, "too_long"),
            detect_bloat.BloatReport("function", source, 6, 4, "too_long"),
        ],
        tmp_path,
    )

    regressions = detect_bloat.find_regressions(
        [
            detect_bloat.BloatReport("function", source, 7, 4, "too_long"),
            detect_bloat.BloatReport("function", source, 6, 4, "too_long"),
        ],
        detect_bloat.load_baseline(baseline),
        tmp_path,
    )

    assert [(item.report.lines, item.previous_lines) for item in regressions] == [
        (7, 5)
    ]


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("[tool]\n", "missing [tool.detect-bloat]"),
        (
            """
[tool.detect-bloat]
code_paths = []
test_paths = ["tests"]
skills_path = "skills"
code_file_lines = 100
test_file_lines = 100
skill_directory_lines = 50
function_lines = 10
""",
            "code_paths must be a non-empty string list",
        ),
        (
            """
[tool.detect-bloat]
code_paths = ["orchestune"]
test_paths = ["tests"]
skills_path = "skills"
code_file_lines = 0
test_file_lines = 100
skill_directory_lines = 50
function_lines = 10
""",
            "code_file_lines must be a positive integer",
        ),
    ],
)
def test_load_config_rejects_invalid_values(
    tmp_path: Path, config: str, error: str
) -> None:
    detect_bloat = _load_script()
    tmp_path.joinpath("pyproject.toml").write_text(config.lstrip(), encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        detect_bloat.load_config(tmp_path)
    assert error in str(exc_info.value)
