"""Strict read-validation blocks and their round/report/candidate bindings."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from orchestune.read_dependencies.contracts import CandidateConclusion
from orchestune.read_dependencies.validation import (
    parse_read_validation,
    validate_read_validation,
)

HEAD = "a" * 40
DIGEST = "b" * 64
CANDIDATE = "sr1-" + "c" * 64
OTHER = "sr1-" + "d" * 64


def payload(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "round_number": 2,
        "head_sha": HEAD,
        "report_digest": DIGEST,
        "decisions": [
            {
                "candidate_id": CANDIDATE,
                "conclusion": "compatible",
                "rationale": "Checked reader against writer",
            }
        ],
        **changes,
    }


def block(data: object) -> str:
    return "```orchestune-read-validation\n" + json.dumps(data) + "\n```"


def check(value: Any, candidates: tuple[str, ...] = (CANDIDATE,)) -> None:
    validate_read_validation(
        value,
        round_number=2,
        head_sha=HEAD,
        report_digest=DIGEST,
        candidate_ids=candidates,
    )


@pytest.mark.parametrize("conclusion", ["compatible", "repaired", "unknown"])
def test_parse_preserves_each_conclusion_and_rationale(conclusion: str) -> None:
    data = payload()
    data["decisions"][0]["conclusion"] = conclusion
    value = parse_read_validation("Review complete\n\n" + block(data) + "\n")
    assert value.round_number == 2 and value.head_sha == HEAD
    assert value.report_digest == DIGEST
    assert value.decisions[0].conclusion is CandidateConclusion(conclusion)
    assert value.decisions[0].rationale == "Checked reader against writer"


@pytest.mark.parametrize(
    "data",
    [
        [],
        None,
        {},
        payload(extra=True),
        payload(version=True),
        payload(version=1.0),
        payload(version=2),
        payload(round_number=True),
        payload(round_number=0),
        payload(round_number="2"),
        payload(head_sha="A" * 40),
        payload(report_digest="x"),
        payload(decisions={}),
        payload(decisions=[None]),
    ],
)
def test_parser_rejects_wrong_structure(data: object) -> None:
    with pytest.raises(ValueError):
        parse_read_validation(block(data))


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_id": 1},
        {"candidate_id": "bad"},
        {"conclusion": "pass"},
        {"conclusion": True},
        {"rationale": " "},
        {"rationale": None},
        {"extra": "x"},
    ],
)
def test_parser_rejects_bad_decisions(change: dict[str, Any]) -> None:
    data = payload()
    data["decisions"][0].update(change)
    with pytest.raises(ValueError):
        parse_read_validation(block(data))


@pytest.mark.parametrize(
    "body",
    [
        "",
        "```orchestune-read-validation\n{}",
        block(payload()) + "\n" + block(payload()),
        '```orchestune-read-validation\n{"version": 1, "version": 1}\n```',
        '```orchestune-read-validation\n{"version": NaN}\n```',
        '```orchestune-read-validation\n{"version": Infinity}\n```',
        "```orchestune-read-validation\nversion: 1\n```",
    ],
)
def test_parser_requires_one_strict_json_block(body: str) -> None:
    with pytest.raises(ValueError):
        parse_read_validation(body)


def test_parser_rejects_duplicate_candidate_decisions() -> None:
    data = payload()
    data["decisions"] *= 2
    with pytest.raises(ValueError, match="unique"):
        parse_read_validation(block(data))


def test_candidate_binding_requires_exact_coverage_even_without_findings() -> None:
    value = parse_read_validation(block(payload()))
    check(value)
    for candidate_ids in [(), (OTHER,), (CANDIDATE, OTHER), (CANDIDATE, CANDIDATE)]:
        with pytest.raises(ValueError):
            check(value, candidate_ids)
    with pytest.raises(ValueError):
        check(replace(value, decisions=()))
    check(replace(value, decisions=()), ())


@pytest.mark.parametrize(
    "changes",
    [{"round_number": 1}, {"head_sha": "e" * 40}, {"report_digest": "f" * 64}],
)
def test_validation_rejects_stale_round_head_or_digest(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        check(parse_read_validation(block(payload(**changes))))


def test_unknown_is_valid_data_but_never_a_pass() -> None:
    value = parse_read_validation(block(payload()))
    unknown = replace(value.decisions[0], conclusion=CandidateConclusion.UNKNOWN)
    with pytest.raises(ValueError, match="unknown"):
        check(replace(value, decisions=(unknown,)))
