"""共有ドキュメントのfootprint完全一致による競合検出(#724)。

類似度はtouch_set全体の加重コサイン類似度であり、多数のタスクが触る文書は
IDFで相対的な重みが小さくなるため、同じ文書をfootprintへ宣言していても閾値未満
で競合辺が出ないことがある。ここでは類似度とは独立に、リポジトリルート`docs/`
配下のMarkdownを「正規化パスの完全一致」で排他する。

`contracts.py`のカテゴリ（親ディレクトリ単位のグルーピング）は文書へ適用しない:
同じディレクトリの別文書（`docs/ja/a.md`と`docs/ja/b.md`）まで排他になるため。
日英の別文書も同様に別ファイルとして扱う。排他だけを追加し、`depends_on`や所有者
は推定しない。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from itertools import combinations
from typing import Protocol

from orchestune.dag.models import (
    ConflictEdge,
    SubTask,
    is_ignored_footprint,
    normalize_footprint_path,
)

SHARED_DOCUMENT_REASON = "shared-document"
_DOCUMENT_ROOT = "docs/"
_DOCUMENT_SUFFIX = ".md"


class FootprintTask(Protocol):
    """The metadata required to inspect declared footprint paths."""

    @property
    def footprint(self) -> tuple[str, ...]: ...


def _document_path(path: str) -> str | None:
    """対象文書なら正規化パスを、そうでなければNoneを返す。

    パス・拡張子は大文字小文字を区別する。実在確認やsymlink解決はしない。
    """
    try:
        normalized = normalize_footprint_path(path)
    except ValueError:
        return None
    if not normalized.startswith(_DOCUMENT_ROOT):
        return None
    name = posixpath.basename(normalized)
    if not name.endswith(_DOCUMENT_SUFFIX) or name == _DOCUMENT_SUFFIX:
        return None
    return normalized


def is_shared_document_path(path: str) -> bool:
    return _document_path(path) is not None


def declares_shared_document(subtask: FootprintTask) -> bool:
    return any(is_shared_document_path(path) for path in subtask.footprint)


def build_shared_document_conflicts(
    subtasks: list[SubTask],
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> list[ConflictEdge]:
    """同じ文書を宣言した異なるタスクの全ペアへ対称な排他辺を返す。

    `depends_on`で順序済みのペアにも辺を残す（排他と先行関係は別の事実）。
    複数文書を共有するペアは、共有パスをresourcesへまとめた1本の辺にする。
    `ignore_patterns`は自動検出だけに作用する。
    """
    ignore_patterns = tuple(ignore_patterns)
    owners: dict[str, set[str]] = {}
    for subtask in subtasks:
        for path in subtask.footprint:
            document = _document_path(path)
            if document is None or is_ignored_footprint(document, ignore_patterns):
                continue
            owners.setdefault(document, set()).add(subtask.id)

    shared: dict[tuple[str, str], set[str]] = {}
    for document, ids in owners.items():
        for pair in combinations(sorted(ids), 2):
            shared.setdefault(pair, set()).add(document)

    return [
        ConflictEdge(
            left,
            right,
            reason=SHARED_DOCUMENT_REASON,
            resources=tuple(sorted(documents)),
        )
        for (left, right), documents in sorted(shared.items())
    ]
