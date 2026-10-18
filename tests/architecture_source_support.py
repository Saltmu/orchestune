"""Low-level source and AST caching for architecture tests."""

from __future__ import annotations

import ast
import functools
from pathlib import Path


@functools.cache
def _cached_source_text(resolved_path: Path) -> str:
    return resolved_path.read_text(encoding="utf-8")


@functools.cache
def _cached_source_ast(resolved_path: Path) -> ast.Module:
    source = _cached_source_text(resolved_path)
    return ast.parse(source, filename=str(resolved_path))


def _source_text(path: Path) -> str:
    """Return the UTF-8 text of the file at `path`, caching the result per resolved path."""
    return _cached_source_text(Path(path).resolve())


def _source_ast(path: Path) -> ast.Module:
    """Return the parsed AST of the file at `path`, caching the result per resolved path."""
    return _cached_source_ast(Path(path).resolve())


def _clear_source_caches() -> None:
    """Clear source text and AST caches."""
    _cached_source_text.cache_clear()
    _cached_source_ast.cache_clear()
