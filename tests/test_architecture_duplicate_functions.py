"""Architecture test: prevent duplicate identical functions in orchestune/."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


def _function_body_without_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def test_no_duplicate_functions_over_600_ast_chars() -> None:
    """Ensure no two functions in orchestune/ have identical AST bodies > 600 chars.

    Issue #1585: deduplicate identical functions across the codebase and prevent
    future regressions.
    """
    repo_root = Path(__file__).resolve().parent.parent
    src_root = repo_root / "orchestune"

    dump_to_locations: dict[str, list[tuple[str, str, int]]] = defaultdict(list)

    for py_file in sorted(src_root.rglob("*.py")):
        rel_path = py_file.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(py_file.read_text("utf-8"), filename=rel_path)
        except Exception as exc:
            raise AssertionError(f"Failed to parse {rel_path}: {exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                body = _function_body_without_docstring(node)
                body_dump = "".join(ast.dump(stmt) for stmt in body)
                if len(body_dump) > 600:
                    dump_to_locations[body_dump].append(
                        (rel_path, node.name, node.lineno)
                    )

    duplicates = {k: v for k, v in dump_to_locations.items() if len(v) > 1}
    if duplicates:
        failure_lines = ["Found duplicate functions with AST body > 600 chars:"]
        for dump_str, locs in duplicates.items():
            loc_descs = [f"{path}:{line} ({name})" for path, name, line in locs]
            failure_lines.append(f"  - Length {len(dump_str)}: {', '.join(loc_descs)}")
        raise AssertionError("\n".join(failure_lines))
