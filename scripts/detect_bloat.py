#!/usr/bin/env python3
"""Report oversized source files, functions, tests, and skill directories."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_SECTION = "detect-bloat"


@dataclass(frozen=True)
class BloatConfig:
    code_paths: tuple[Path, ...]
    test_paths: tuple[Path, ...]
    skills_path: Path
    code_file_lines: int
    test_file_lines: int
    skill_directory_lines: int
    function_lines: int


@dataclass(frozen=True)
class BloatReport:
    category: str
    path: Path
    lines: int
    limit: int
    symbol: str | None = None


@dataclass(frozen=True)
class BloatRegression:
    """A warning newly introduced or grown since the recorded baseline."""

    report: BloatReport
    previous_lines: int | None


def _collect_docstring_lines(tree: ast.AST) -> set[int]:
    """Collect line numbers occupied by docstrings (module, class, function)."""
    doc_lines: set[int] = set()

    def _add_docstring(body: list[ast.stmt]) -> None:
        if body and isinstance(body[0], ast.Expr):
            val = body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                start = body[0].lineno
                end = body[0].end_lineno or start
                doc_lines.update(range(start, end + 1))

    if isinstance(tree, ast.Module):
        _add_docstring(tree.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            _add_docstring(node.body)

    return doc_lines


def _count_sloc(
    source_lines: Sequence[str],
    doc_lines: set[int],
    start_line: int = 1,
    end_line: int | None = None,
) -> int:
    """Count non-empty, non-comment, non-docstring lines in the specified range."""
    end = len(source_lines) if end_line is None else min(end_line, len(source_lines))
    sloc = 0
    for line_no in range(start_line, end + 1):
        if line_no in doc_lines:
            continue
        line_idx = line_no - 1
        if 0 <= line_idx < len(source_lines):
            stripped = source_lines[line_idx].strip()
            if not stripped or stripped.startswith("#"):
                continue
            sloc += 1
    return sloc


class FunctionLineCounter(ast.NodeVisitor):
    """Collect Python functions that exceed the configured SLOC limit."""

    def __init__(
        self,
        limit: int,
        source_lines: Sequence[str],
        doc_lines: set[int],
    ) -> None:
        self._limit = limit
        self._source_lines = source_lines
        self._doc_lines = doc_lines
        self._class_names: list[str] = []
        self.functions: list[tuple[str, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_names.append(node.name)
        self.generic_visit(node)
        self._class_names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_if_large(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_if_large(node)
        self.generic_visit(node)

    def _record_if_large(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        start = node.lineno
        end = node.end_lineno or start
        sloc = _count_sloc(self._source_lines, self._doc_lines, start, end)
        if sloc <= self._limit:
            return
        prefix = ".".join(self._class_names)
        name = f"{prefix}.{node.name}" if prefix else node.name
        self.functions.append((name, sloc))


def _read_required_int(config: dict[str, Any], name: str) -> int:
    value = config.get(name)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"tool.{CONFIG_SECTION}.{name} must be a positive integer")
    return value


def _read_paths(config: dict[str, Any], name: str) -> tuple[Path, ...]:
    value = config.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(
            f"tool.{CONFIG_SECTION}.{name} must be a non-empty string list"
        )
    return tuple(Path(item) for item in value)


def load_config(root_dir: Path) -> BloatConfig:
    """Load all scan paths and thresholds from ``pyproject.toml``."""

    pyproject = root_dir / "pyproject.toml"
    try:
        with pyproject.open("rb") as config_file:
            document = tomllib.load(config_file)
    except OSError as exc:
        raise ValueError(f"cannot read {pyproject}: {exc}") from exc

    tool = document.get("tool")
    section = tool.get(CONFIG_SECTION) if isinstance(tool, dict) else None
    if not isinstance(section, dict):
        raise ValueError(f"missing [tool.{CONFIG_SECTION}] in {pyproject}")

    skills_path = section.get("skills_path")
    if not isinstance(skills_path, str) or not skills_path:
        raise ValueError(
            f"tool.{CONFIG_SECTION}.skills_path must be a non-empty string"
        )

    return BloatConfig(
        code_paths=_read_paths(section, "code_paths"),
        test_paths=_read_paths(section, "test_paths"),
        skills_path=Path(skills_path),
        code_file_lines=_read_required_int(section, "code_file_lines"),
        test_file_lines=_read_required_int(section, "test_file_lines"),
        skill_directory_lines=_read_required_int(section, "skill_directory_lines"),
        function_lines=_read_required_int(section, "function_lines"),
    )


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as source_file:
            return sum(1 for _ in source_file)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[detect-bloat] Cannot read {path}: {exc}", file=sys.stderr)
        return 0


def _iter_python_files(
    root_dir: Path, relative_paths: Iterable[Path]
) -> Iterable[Path]:
    seen: set[Path] = set()
    for relative_path in relative_paths:
        directory = root_dir / relative_path
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            if path not in seen:
                seen.add(path)
                yield path


def _scan_code_file(path: Path, config: BloatConfig) -> list[BloatReport]:
    try:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"[detect-bloat] Cannot parse {path}: {exc}", file=sys.stderr)
        lines = _line_count(path)
        return (
            [BloatReport("code", path, lines, config.code_file_lines)]
            if lines > config.code_file_lines
            else []
        )

    source_lines = source_text.splitlines()
    doc_lines = _collect_docstring_lines(tree)
    file_sloc = _count_sloc(source_lines, doc_lines)

    reports: list[BloatReport] = []
    if file_sloc > config.code_file_lines:
        reports.append(BloatReport("code", path, file_sloc, config.code_file_lines))

    counter = FunctionLineCounter(config.function_lines, source_lines, doc_lines)
    counter.visit(tree)
    reports.extend(
        BloatReport("function", path, line_count, config.function_lines, symbol)
        for symbol, line_count in counter.functions
    )
    return reports


def _scan_test_file(path: Path, config: BloatConfig) -> list[BloatReport]:
    try:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"[detect-bloat] Cannot parse {path}: {exc}", file=sys.stderr)
        lines = _line_count(path)
        return (
            [BloatReport("test", path, lines, config.test_file_lines)]
            if lines > config.test_file_lines
            else []
        )

    source_lines = source_text.splitlines()
    doc_lines = _collect_docstring_lines(tree)
    file_sloc = _count_sloc(source_lines, doc_lines)

    if file_sloc > config.test_file_lines:
        return [BloatReport("test", path, file_sloc, config.test_file_lines)]
    return []


def scan_project(
    root_dir: Path, config: BloatConfig | None = None
) -> list[BloatReport]:
    """Return every configured bloat warning without changing the project."""

    config = load_config(root_dir) if config is None else config
    reports: list[BloatReport] = []

    for path in _iter_python_files(root_dir, config.code_paths):
        reports.extend(_scan_code_file(path, config))

    for path in _iter_python_files(root_dir, config.test_paths):
        reports.extend(_scan_test_file(path, config))

    skills_dir = root_dir / config.skills_path
    if skills_dir.is_dir():
        # Stable report ordering keeps CLI output and tests deterministic.
        for skill_dir in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
            lines = sum(_line_count(path) for path in skill_dir.rglob("*.md"))
            if lines > config.skill_directory_lines:
                reports.append(
                    BloatReport("skill", skill_dir, lines, config.skill_directory_lines)
                )

    return reports


def _render_report(
    report: BloatReport, root_dir: Path, previous_lines: int | None = None
) -> str:
    path = report.path.relative_to(root_dir).as_posix()
    previous = f" (was {previous_lines}; " if previous_lines is not None else " ("
    if report.category == "function":
        return (
            f"[function] {path}:{report.symbol} has {report.lines} lines "
            f"{previous}limit: {report.limit})"
        )
    return f"[{report.category}] {path} has {report.lines} lines{previous}limit: {report.limit})"


def _warning_snapshot(report: BloatReport, root_dir: Path) -> dict[str, object]:
    return {
        "category": report.category,
        "limit": report.limit,
        "lines": report.lines,
        "path": report.path.relative_to(root_dir).as_posix(),
        "symbol": report.symbol,
    }


def _warning_key(snapshot: dict[str, object]) -> tuple[str, str, str | None]:
    category = snapshot.get("category")
    path = snapshot.get("path")
    symbol = snapshot.get("symbol")
    if not isinstance(category, str) or not isinstance(path, str):
        raise ValueError("baseline warning must include string category and path")
    if symbol is not None and not isinstance(symbol, str):
        raise ValueError("baseline warning symbol must be a string or null")
    return category, path, symbol


def write_baseline(path: Path, reports: Sequence[BloatReport], root_dir: Path) -> None:
    """Write a stable JSON snapshot of the current bloat warnings."""

    warnings = [_warning_snapshot(report, root_dir) for report in reports]
    warnings.sort(key=_warning_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as baseline_file:
        json.dump(
            {"version": 1, "warnings": warnings},
            baseline_file,
            indent=2,
            sort_keys=True,
        )
        baseline_file.write("\n")
    temporary_path.replace(path)


def load_baseline(path: Path) -> list[dict[str, object]]:
    """Read and validate a baseline created by :func:`write_baseline`."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("baseline must contain version 1")
    warnings = document.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, dict) for item in warnings
    ):
        raise ValueError("baseline warnings must be a list")
    for warning in warnings:
        _warning_key(warning)
        if not isinstance(warning.get("lines"), int):
            raise ValueError("baseline warning lines must be an integer")
    return warnings


def find_regressions(
    reports: Sequence[BloatReport],
    baseline: Sequence[dict[str, object]],
    root_dir: Path,
) -> list[BloatRegression]:
    """Return warnings absent from, or larger than, the recorded baseline."""

    baseline_lines: dict[tuple[str, str, str | None], list[int]] = {}
    for warning in baseline:
        key = _warning_key(warning)
        line_count = warning.get("lines")
        if not isinstance(line_count, int):
            raise ValueError("baseline warning lines must be an integer")
        baseline_lines.setdefault(key, []).append(line_count)
    for lines in baseline_lines.values():
        lines.sort()

    regressions: list[BloatRegression] = []
    for report in sorted(
        reports,
        key=lambda item: (_warning_key(_warning_snapshot(item, root_dir)), item.lines),
    ):
        snapshot = _warning_snapshot(report, root_dir)
        candidates = baseline_lines.get(_warning_key(snapshot), [])
        previous_lines = next(
            (line for line in candidates if line >= report.lines), None
        )
        if previous_lines is not None:
            candidates.remove(previous_lines)
            continue
        if candidates:
            previous_lines = candidates.pop()
        if previous_lines is None or report.lines > previous_lines:
            regressions.append(BloatRegression(report, previous_lines))
    return regressions


def _render_regression(regression: BloatRegression, root_dir: Path) -> str:
    if regression.previous_lines is None:
        return f"[new] {_render_report(regression.report, root_dir)}"
    return "[worsened] " + _render_report(
        regression.report, root_dir, regression.previous_lines
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="project root to scan")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--warn-only",
        action="store_true",
        help="report warnings without failing the command",
    )
    mode.add_argument(
        "--record-baseline",
        type=Path,
        metavar="PATH",
        help="write the current warnings to a JSON baseline",
    )
    mode.add_argument(
        "--baseline",
        type=Path,
        metavar="PATH",
        help="fail only for warnings that are new or worse than this JSON baseline",
    )
    args = parser.parse_args(argv)
    root_dir = Path(args.root).resolve()

    try:
        reports = scan_project(root_dir)
    except ValueError as exc:
        print(f"[detect-bloat] Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.record_baseline:
        try:
            write_baseline(args.record_baseline, reports, root_dir)
        except OSError as exc:
            print(f"[detect-bloat] Baseline error: {exc}", file=sys.stderr)
            return 2
        print(f"[detect-bloat] Recorded baseline: {args.record_baseline}")
        return 0

    if args.baseline:
        try:
            regressions = find_regressions(
                reports, load_baseline(args.baseline), root_dir
            )
        except ValueError as exc:
            print(f"[detect-bloat] Baseline error: {exc}", file=sys.stderr)
            return 2
        if not regressions:
            print("[detect-bloat] No new or worsened bloat warnings.")
            return 0
        print("[detect-bloat] New or worsened bloat warnings:")
        for regression in regressions:
            print(_render_regression(regression, root_dir))
        return 1

    if not reports:
        print("[detect-bloat] No bloat detected.")
        return 0

    print("[detect-bloat] Bloat warnings:")
    for report in reports:
        print(_render_report(report, root_dir))
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
