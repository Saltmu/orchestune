"""BM25 ranking stays pure Python and independent of SQLite FTS5."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from orchestune.context.budget import (
    DEFAULT_CLOUD_PROMPT_MAX_BYTES,
    DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
    DEFAULT_TIER2_LIMIT,
    DEFAULT_TIER2_MIN_SCORE,
    DEFAULT_TIER2_MIN_SCORE_RATIO,
    fit_context_budget,
)
from orchestune.context.search import (
    BM25_B,
    BM25_K1,
    BM25_PARAMS_VERSION,
    TOKENIZER_VERSION,
    search_artifacts,
)


def _doc(
    issue: int,
    comment: str,
    *,
    title: str = "",
    description: str = "",
    footprint: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    summary: str = "",
    task_kind: str = "unspecified",
    completion_id: str | None = "completion",
) -> SimpleNamespace:
    return SimpleNamespace(
        issue_number=issue,
        source_comment_id=comment,
        completion_id=completion_id,
        title=title,
        description=description,
        footprint=footprint,
        symbols=symbols,
        summary=summary,
        task_kind=task_kind,
    )


def test_versions_are_fixed() -> None:
    assert TOKENIZER_VERSION == "char-bigram-v1"
    assert BM25_PARAMS_VERSION == "bm25-v1"
    assert BM25_K1 == 1.2
    assert BM25_B == 0.75


def test_japanese_query_uses_normalized_character_bigrams() -> None:
    hits = search_artifacts(
        (
            _doc(2, "c2", title="索引検索"),
            _doc(1, "c1", title="天気"),
            _doc(3, "c3", title="ＡＢＣ検索"),
        ),
        "検索",
    )

    assert [hit.issue_number for hit in hits] == [2, 3]


def test_one_character_token_is_a_unigram() -> None:
    hits = search_artifacts((_doc(4, "c4", title="索"),), "索")

    assert [hit.issue_number for hit in hits] == [4]


def test_symbols_and_paths_split_on_delimiters() -> None:
    hits = search_artifacts(
        (
            _doc(
                8,
                "c8",
                footprint=("orchestune/context/search.py",),
                symbols=("GitHubIssueMixin.list_issues_by_label",),
            ),
        ),
        "list_issues",
    )

    assert [hit.issue_number for hit in hits] == [8]


def test_ties_use_stable_identity_order() -> None:
    docs = (
        _doc(2, "b", title="索引検索", completion_id="m"),
        _doc(2, "a", title="索引検索", completion_id="z"),
        _doc(2, "a", title="索引検索", completion_id="a"),
        _doc(1, "z", title="索引検索", completion_id=None),
    )

    hits = search_artifacts(docs, "検索")

    assert [
        (hit.issue_number, hit.source_comment_id, hit.completion_id) for hit in hits
    ] == [
        (1, "z", None),
        (2, "a", "a"),
        (2, "a", "z"),
        (2, "b", "m"),
    ]


def test_zero_hits_and_empty_query_return_nothing() -> None:
    docs = (_doc(1, "c1", title="索引検索"),)

    assert search_artifacts(docs, "存在しない語") == ()
    assert search_artifacts(docs, "") == ()
    assert search_artifacts((), "検索") == ()


def test_default_limit_keeps_five_stable_hits() -> None:
    docs = tuple(_doc(issue, "c", title="索引検索") for issue in range(1, 7))

    hits = search_artifacts(docs, "検索")

    assert [hit.issue_number for hit in hits] == [1, 2, 3, 4, 5]


def test_limit_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        search_artifacts((), "検索", limit=-1)


def test_score_bounds_keep_the_stronger_hit() -> None:
    query = "索引検索の予算を注入する"
    strong = _doc(1, "c1", title=query * 4)
    weak = _doc(2, "c2", title="引検")

    assert [hit.issue_number for hit in search_artifacts((strong, weak), query)] == [
        1,
        2,
    ]
    floored = search_artifacts((strong, weak), query, min_score=1.0)
    ratio = search_artifacts((strong, weak), query, min_score_ratio=0.5)

    assert [hit.issue_number for hit in floored] == [1]
    assert [hit.issue_number for hit in ratio] == [1]


@pytest.mark.parametrize(
    "value",
    [-0.1, True, float("nan"), float("inf")],
)
def test_min_score_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        search_artifacts((), "検索", min_score=cast(float, value))


@pytest.mark.parametrize(
    "value",
    [-0.1, 1.1, True, float("nan"), float("-inf")],
)
def test_min_score_ratio_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        search_artifacts((), "検索", min_score_ratio=cast(float, value))


def test_fts5_availability_does_not_change_rank() -> None:
    docs = (
        _doc(2, "c2", title="索引検索"),
        _doc(1, "c1", summary="検索の予算"),
    )
    before = search_artifacts(docs, "検索")
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        connection.execute("INSERT INTO docs VALUES ('索引検索')")
    except sqlite3.OperationalError:
        pass

    assert search_artifacts(docs, "検索") == before


def test_budget_measures_utf8_bytes_without_a_tokenizer_cap() -> None:
    assert DEFAULT_TIER2_LIMIT == 3
    assert DEFAULT_TIER2_MIN_SCORE == 1.0
    assert DEFAULT_TIER2_MIN_SCORE_RATIO == 0.5
    assert DEFAULT_LOCAL_CONTEXT_MAX_BYTES == 32768
    assert DEFAULT_CLOUD_PROMPT_MAX_BYTES == 8192
    local = fit_context_budget("あ")
    assert local.size_bytes == 3
    assert local.fits is True
    assert fit_context_budget("あ", max_bytes=2).fits is False
    payload = fit_context_budget(
        {"text": "あ"}, max_bytes=DEFAULT_CLOUD_PROMPT_MAX_BYTES
    )
    assert payload.size_bytes == len('{"text":"あ"}'.encode())
    assert payload.fits is True
