#!/usr/bin/env python3
"""Report oversized source files, functions, tests, and skill directories."""

from __future__ import annotations

import argparse
import ast
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


class FunctionLineCounter(ast.NodeVisitor):
    """Collect Python functions that exceed the configured line limit."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
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
        end_line = node.end_lineno or node.lineno
        line_count = end_line - node.lineno + 1
        if line_count <= self._limit:
            return
        prefix = ".".join(self._class_names)
        name = f"{prefix}.{node.name}" if prefix else node.name
        self.functions.append((name, line_count))


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


def _function_reports(path: Path, limit: int) -> list[BloatReport]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"[detect-bloat] Cannot parse {path}: {exc}", file=sys.stderr)
        return []

    counter = FunctionLineCounter(limit)
    counter.visit(tree)
    return [
        BloatReport("function", path, line_count, limit, symbol)
        for symbol, line_count in counter.functions
    ]


def scan_project(
    root_dir: Path, config: BloatConfig | None = None
) -> list[BloatReport]:
    """Return every configured bloat warning without changing the project."""

    config = load_config(root_dir) if config is None else config
    reports: list[BloatReport] = []

    for path in _iter_python_files(root_dir, config.code_paths):
        lines = _line_count(path)
        if lines > config.code_file_lines:
            reports.append(BloatReport("code", path, lines, config.code_file_lines))
        # Function length is intentionally independent of file length.
        reports.extend(_function_reports(path, config.function_lines))

    for path in _iter_python_files(root_dir, config.test_paths):
        lines = _line_count(path)
        if lines > config.test_file_lines:
            reports.append(BloatReport("test", path, lines, config.test_file_lines))

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


def _render_report(report: BloatReport, root_dir: Path) -> str:
    path = report.path.relative_to(root_dir)
    if report.category == "function":
        return (
            f"[function] {path}:{report.symbol} has {report.lines} lines "
            f"(limit: {report.limit})"
        )
    return (
        f"[{report.category}] {path} has {report.lines} lines (limit: {report.limit})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="project root to scan")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="report warnings without failing the command",
    )
    args = parser.parse_args(argv)
    root_dir = Path(args.root).resolve()

    try:
        reports = scan_project(root_dir)
    except ValueError as exc:
        print(f"[detect-bloat] Configuration error: {exc}", file=sys.stderr)
        return 2

    if not reports:
        print("[detect-bloat] No bloat detected.")
        return 0

    print("[detect-bloat] Bloat warnings:")
    for report in reports:
        print(_render_report(report, root_dir))
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
