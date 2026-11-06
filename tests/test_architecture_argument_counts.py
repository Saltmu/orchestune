"""Architectural invariant verifying PLR0913 baseline matches AST-collected noqa markers."""

from __future__ import annotations

import ast
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / ".orchestune" / "plr0913-baseline.txt"
SCAN_DIRECTORIES = ["orchestune", "scripts"]

_NOQA_PLR0913_RE = re.compile(r"#\s*noqa(?::\s*([A-Za-z0-9_,\s]+))?")


def has_plr0913_noqa(comment: str) -> bool:
    """Return True if comment suppresses PLR0913."""
    match = _NOQA_PLR0913_RE.search(comment)
    if not match:
        return False
    codes = match.group(1)
    if codes is None:
        return True
    return any(code.strip() == "PLR0913" for code in codes.split(","))


def collect_plr0913_noqa_functions(base_dirs: list[str]) -> list[str]:
    """Collect all functions in base_dirs that have '# noqa: PLR0913' in their def signature."""
    records: list[str] = []

    for base_dir_name in base_dirs:
        base_dir = REPO_ROOT / base_dir_name
        if not base_dir.exists():
            continue

        for py_path in sorted(base_dir.rglob("*.py")):
            content = py_path.read_text(encoding="utf-8")
            with open(py_path, "rb") as f:
                try:
                    tokens = list(tokenize.tokenize(f.readline))
                except Exception:  # noqa: BLE001 - boundary: test_plr0913_baseline_matches skips unreadable token streams
                    continue

            noqa_lines = {
                t.start[0]
                for t in tokens
                if t.type == tokenize.COMMENT and has_plr0913_noqa(t.string)
            }

            if not noqa_lines:
                continue

            try:
                tree = ast.parse(content, filename=str(py_path))
            except Exception:  # noqa: BLE001 - boundary: test_plr0913_baseline_matches skips sources the AST parser cannot inspect
                continue

            rel_path = py_path.relative_to(REPO_ROOT).as_posix()

            class Visitor(ast.NodeVisitor):
                def __init__(
                    self,
                    target_noqa_lines: set[int],
                    target_rel_path: str,
                ) -> None:
                    self.scope: list[str] = []
                    self.target_noqa_lines = target_noqa_lines
                    self.target_rel_path = target_rel_path

                def visit_ClassDef(self, node: ast.ClassDef) -> None:
                    self.scope.append(node.name)
                    self.generic_visit(node)
                    self.scope.pop()

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self._check_function(node)

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                    self._check_function(node)

                def _check_function(
                    self, node: ast.FunctionDef | ast.AsyncFunctionDef
                ) -> None:
                    header_end = node.body[0].lineno if node.body else node.lineno
                    fn_header_lines = set(range(node.lineno, header_end + 1))
                    if fn_header_lines & self.target_noqa_lines:
                        qname = ".".join(self.scope + [node.name])
                        records.append(f"{self.target_rel_path}:{qname}")
                    self.scope.append(node.name)
                    self.generic_visit(node)
                    self.scope.pop()

            Visitor(noqa_lines, rel_path).visit(tree)

    return sorted(records)


def load_plr0913_baseline(path: Path = BASELINE_PATH) -> list[str]:
    """Load baseline list of functions from text file.

    If baseline file does not exist, returns empty list (all baselined functions refactored).
    """
    if not path.exists():
        return []
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return sorted(lines)


def test_plr0913_baseline_matches() -> None:
    """Verify that AST-collected # noqa: PLR0913 functions match the baseline exactly."""
    baseline = load_plr0913_baseline()
    actual = collect_plr0913_noqa_functions(SCAN_DIRECTORIES)

    missing = set(baseline) - set(actual)
    extra = set(actual) - set(baseline)

    error_messages = []
    if missing:
        error_messages.append(
            f"Functions in baseline but missing # noqa: PLR0913 (or refactored): {sorted(missing)}"
        )
    if extra:
        error_messages.append(
            f"Functions with # noqa: PLR0913 but not recorded in baseline: {sorted(extra)}"
        )

    assert not error_messages, "\n".join(error_messages)
    assert actual == baseline, "Baseline order or contents do not match exactly"


class TestPlr0913CollectorUnit:
    def test_has_plr0913_noqa(self) -> None:
        assert has_plr0913_noqa("# noqa: PLR0913")
        assert has_plr0913_noqa("# noqa: E501, PLR0913")
        assert has_plr0913_noqa("# noqa")
        assert not has_plr0913_noqa("# noqa: E501")
        assert not has_plr0913_noqa("# plain comment")

    def test_load_plr0913_baseline_missing_file(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "nonexistent.txt"
        assert load_plr0913_baseline(missing_file) == []

    def test_load_plr0913_baseline_skips_empty_and_comment_lines(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "baseline.txt"
        sample.write_text(
            "\n# comment line\npath/to/file.py:fn\n\npath/to/another.py:Cls.method\n",
            encoding="utf-8",
        )
        loaded = load_plr0913_baseline(sample)
        assert loaded == ["path/to/another.py:Cls.method", "path/to/file.py:fn"]
