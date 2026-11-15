"""Rank passed artifact records with character bigrams and BM25.

The score is computed in Python from the records the caller already holds.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Any

TOKENIZER_VERSION = "char-bigram-v1"
BM25_PARAMS_VERSION = "bm25-v1"
BM25_K1 = 1.2
BM25_B = 0.75
_DEFAULT_LIMIT = 5
_TEXT_FIELDS = ("title", "description", "summary", "task_kind")
_IDENTIFIER_FIELDS = ("footprint", "symbols")
_IDENTIFIER_SPLIT = re.compile(r"[/._\-:\s]+")


def search_artifacts(
    artifacts: Sequence[Any], query: str, *, limit: int = _DEFAULT_LIMIT
) -> tuple[Any, ...]:
    """Return up to ``limit`` records with a positive BM25 score.

    Equal scores keep issue number, source comment id, then completion id.
    A missing completion id sorts before any stored id. A negative limit is
    rejected. An empty query or an empty collection returns no hits.
    """
    if limit < 0:
        raise ValueError("limit must be zero or greater")
    documents = tuple(artifacts)
    query_counts = _token_counts(query)
    if not documents or not query_counts or limit == 0:
        return ()
    ranked = sorted(_scored(documents, query_counts), key=_tie_key)
    return tuple(document for document, _score in ranked[:limit])


def _tie_key(item: tuple[Any, float]) -> tuple[float, int, str, str]:
    document, score = item
    completion = getattr(document, "completion_id", None)
    completion_id = "" if completion is None else str(completion)
    return (
        -score,
        int(getattr(document, "issue_number", 0)),
        str(getattr(document, "source_comment_id", "")),
        completion_id,
    )


def _scored(
    documents: tuple[Any, ...], query_counts: Counter[str]
) -> list[tuple[Any, float]]:
    counts = tuple(_document_counts(document) for document in documents)
    lengths = tuple(sum(count.values()) for count in counts)
    if sum(lengths) == 0:
        return []
    average = sum(lengths) / len(documents)
    frequencies = _document_frequencies(counts)
    scored: list[tuple[Any, float]] = []
    for document, count, length in zip(documents, counts, lengths, strict=True):
        score = _bm25(query_counts, count, length, average, len(documents), frequencies)
        if score > 0:
            scored.append((document, score))
    return scored


def _bm25(
    query_counts: Counter[str],
    document_counts: Counter[str],
    length: int,
    average: float,
    total_documents: int,
    frequencies: dict[str, int],
) -> float:
    score = 0.0
    for term in query_counts:
        frequency = document_counts.get(term, 0)
        if frequency == 0:
            continue
        contained = frequencies.get(term, 0)
        idf = math.log(1 + (total_documents - contained + 0.5) / (contained + 0.5))
        normal = 1 - BM25_B + BM25_B * length / average
        denominator = frequency + BM25_K1 * normal
        score += idf * (frequency * (BM25_K1 + 1)) / denominator
    return score


def _document_frequencies(counts: tuple[Counter[str], ...]) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for count in counts:
        for term in count:
            frequencies[term] = frequencies.get(term, 0) + 1
    return frequencies


def _document_counts(document: Any) -> Counter[str]:
    counts: Counter[str] = Counter()
    for name in (*_TEXT_FIELDS, *_IDENTIFIER_FIELDS):
        for text in _texts(getattr(document, name, None)):
            counts.update(_token_counts(text))
    return counts


def _token_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    counts.update(_char_tokens(text))
    counts.update(_identifier_tokens(text))
    return counts


def _texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, tuple | list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _char_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    if len(normalized) == 1:
        return (normalized,)
    return tuple(normalized[index : index + 2] for index in range(len(normalized) - 1))


def _identifier_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return tuple(part for part in _IDENTIFIER_SPLIT.split(normalized) if part)
