"""footprint の値（リポジトリ相対の POSIX パス）の正規化と検証。

claim・ledger・Issue 解析・DAG が共有する L0 の部品で、標準ライブラリ以外に
依存しない。DAG の衝突判定が無視するファイルの方針（`is_ignored_footprint`）は
DAG 固有なので `dag.models` に置く。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable


def normalize_footprint_path(path: str) -> str:
    """Normalize a footprint path into a canonical repository-relative POSIX path.

    - Converts Windows backslashes '\\' to POSIX forward slashes '/'
    - Rejects absolute paths (starting with '/') and drive-qualified paths (e.g. 'C:')
    - Resolves redundant './', duplicate slashes '//', and relative parent references '..'
    - Raises ValueError if the path is absolute/drive-qualified, resolves to empty/root ('.'), or escapes the repository root
    """
    if not isinstance(path, str):
        path = str(path)

    clean_path = path.replace("\\", "/")

    if re.match(r"^[a-zA-Z]:", clean_path):
        raise ValueError(
            f"Invalid footprint path: '{path}' is a drive-qualified absolute path"
        )
    if clean_path.startswith("/"):
        raise ValueError(f"Invalid footprint path: '{path}' is an absolute path")

    normalized = posixpath.normpath(clean_path)

    if normalized in ("", "."):
        raise ValueError(
            f"Invalid footprint path: '{path}' resolves to empty or root path"
        )

    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Invalid footprint path: '{path}' escapes repository root")

    return normalized


class FootprintValidationError(ValueError):
    """Raised when a footprint value or element fails validation."""


def parse_footprint_value(raw: object) -> tuple[str, ...]:
    """Strictly validate and normalize a footprint value into a deduplicated tuple.

    - Returns () for None or empty list/tuple.
    - Raises FootprintValidationError if raw is a string, non-sequence, or contains
      invalid elements (dict, list, bool).
    - Skips None elements; stringifies numbers (int/float).
    - Normalizes each path with normalize_footprint_path, wrapping failures in FootprintValidationError.
    - Preserves order while eliminating duplicates via dict.fromkeys.
    """
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, list | tuple):
        raise FootprintValidationError(
            f"'footprint' must be a list of paths, got {type(raw).__name__}: {raw!r}"
        )
    normalized: list[str] = []
    for item in raw:
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, str | int | float):
            raise FootprintValidationError(
                f"Invalid footprint element type: {type(item).__name__} ({item!r})"
            )
        try:
            normalized.append(normalize_footprint_path(str(item)))
        except ValueError as e:
            raise FootprintValidationError(str(e)) from e
    return tuple(dict.fromkeys(normalized))


def canonicalize_footprint(paths: Iterable[str]) -> tuple[str, ...]:
    """Leniently normalize footprint paths for persisted data, retaining invalid paths as-is.

    - Applies normalize_footprint_path to each path.
    - Falls back to the raw string if normalization raises ValueError.
    - Preserves order while eliminating duplicates.
    """
    result: list[str] = []
    for p in paths:
        s = str(p)
        try:
            result.append(normalize_footprint_path(s))
        except ValueError:
            result.append(s)
    return tuple(dict.fromkeys(result))
