"""MCP review snapshot v1 input contract (#1210): pure validation, injectable clock."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestune.review.snapshot import (
    DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    EvidenceContractError,
    InsufficientEvidenceError,
    SnapshotEvidence,
    is_snapshot_v1,
    normalize_timestamp,
    parse_utc,
    validate_snapshot,
)

HEAD = "a" * 40
NOW = datetime(2026, 10, 4, 3, 1, 0, tzinfo=UTC)


def snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "snapshot_version": 1,
        "repository": "owner/repo",
        "pr_number": 123,
        "acquisition": {
            "source": "github_mcp",
            "started_at": "2026-10-04T03:00:00Z",
            "observed_at": "2026-10-04T03:00:20Z",
            "head_before": {"sha": HEAD, "fetched_at": "2026-10-04T03:00:00Z"},
            "head_after": {"sha": HEAD, "fetched_at": "2026-10-04T03:00:20Z"},
        },
        "completeness": {
            "issue_comments": "complete",
            "reviews": "complete",
            "inline_comments": "complete",
        },
        "issue_comments": [],
        "reviews": [],
        "inline_comments": [],
    }
    base.update(overrides)
    return base


def validate(value: Any, **kwargs: Any) -> SnapshotEvidence:
    kwargs.setdefault("pr_number", 123)
    kwargs.setdefault("now", NOW)
    return validate_snapshot(value, **kwargs)


def test_valid_snapshot_yields_verified_identity_and_head() -> None:
    evidence = validate(snapshot())
    assert (evidence.repository, evidence.pr_number) == ("owner/repo", 123)
    assert evidence.head_sha == HEAD
    assert evidence.observed_at == "2026-10-04T03:00:20Z"
    assert evidence.completeness == dict.fromkeys(
        ("issue_comments", "reviews", "inline_comments"), "complete"
    )


def test_pr_number_may_come_from_snapshot_when_cli_omits_it() -> None:
    assert validate(snapshot(), pr_number=None).pr_number == 123


def test_is_snapshot_v1_is_decided_by_the_input_version_key_only() -> None:
    assert is_snapshot_v1({"snapshot_version": 1})
    assert is_snapshot_v1({"snapshot_version": 99})
    assert not is_snapshot_v1({"schema_version": 1})
    assert not is_snapshot_v1([])


@pytest.mark.parametrize("version", [2, 0, "1", True, None, 1.0])
def test_unknown_snapshot_version_is_a_contract_error(version: Any) -> None:
    value = snapshot(snapshot_version=version)
    # 1.0 == 1 in Python; the contract is the integer 1, never a float or bool.
    with pytest.raises(EvidenceContractError, match="snapshot_version"):
        validate(value)


def test_acquisition_result_is_not_an_input_snapshot() -> None:
    with pytest.raises(EvidenceContractError, match="acquisition result"):
        validate(snapshot(acquisition_status="acquired", schema_version=1))


@pytest.mark.parametrize("number", [True, False, 0, -1, "123", 1.5, None])
def test_pr_number_must_be_a_positive_integer(number: Any) -> None:
    with pytest.raises(EvidenceContractError, match="pr_number"):
        validate(snapshot(pr_number=number), pr_number=None)


def test_cli_pr_number_must_match_snapshot() -> None:
    with pytest.raises(EvidenceContractError, match="PR"):
        validate(snapshot(), pr_number=124)


@pytest.mark.parametrize("repo", ["owner", "a/b/c", "", "owner/", " owner/repo", 5])
def test_repository_must_be_owner_slash_repo(repo: Any) -> None:
    with pytest.raises(EvidenceContractError, match="repository"):
        validate(snapshot(repository=repo))


@pytest.mark.parametrize(
    "record",
    [
        {"id": 1, "html_url": "https://github.com/other/repo/pull/123#issuecomment-1"},
        {"id": 1, "html_url": "https://github.com/owner/repo/pull/999#issuecomment-1"},
        {"id": 1, "issue_url": "https://api.github.com/repos/owner/other/issues/123"},
        {"id": 1, "pull_request_url": "https://api.github.com/repos/o/r/pulls/123"},
    ],
)
def test_record_urls_must_agree_with_repository_and_pr(record: dict) -> None:
    with pytest.raises(EvidenceContractError, match="belongs to"):
        validate(snapshot(issue_comments=[record]))


def test_matching_and_unrelated_urls_are_accepted() -> None:
    records = [
        {
            "id": 1,
            "html_url": "https://github.com/Owner/Repo/pull/123#issuecomment-1",
            "url": "https://api.github.com/repos/owner/repo/issues/comments/1",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/123",
        }
    ]
    assert validate(snapshot(issue_comments=records)).state["issue_comments"]


@pytest.mark.parametrize("sha", ["a" * 39, "g" * 40, "", 5, "a" * 41])
def test_head_sha_must_be_40_hex(sha: Any) -> None:
    value = snapshot()
    value["acquisition"]["head_after"]["sha"] = sha
    with pytest.raises(EvidenceContractError, match="sha"):
        validate(value)


def test_head_sha_is_normalized_to_lowercase() -> None:
    value = snapshot()
    for key in ("head_before", "head_after"):
        value["acquisition"][key]["sha"] = HEAD.upper()
    assert validate(value).head_sha == HEAD


@pytest.mark.parametrize(
    "stamp", ["2026-10-04T03:00:20", "yesterday", "", 5, "2026-10-04 03:00:20"]
)
def test_acquisition_times_require_rfc3339_with_timezone(stamp: Any) -> None:
    value = snapshot()
    value["acquisition"]["observed_at"] = stamp
    with pytest.raises(EvidenceContractError, match="observed_at"):
        validate(value)


def test_offset_timestamps_are_normalized_to_utc() -> None:
    value = snapshot()
    value["acquisition"]["observed_at"] = "2026-10-04T12:00:20+09:00"
    value["acquisition"]["head_after"]["fetched_at"] = "2026-10-04T12:00:20+09:00"
    assert validate(value).observed_at == "2026-10-04T03:00:20Z"


@pytest.mark.parametrize(
    ("path", "stamp"),
    [
        (("started_at",), "2026-10-04T03:00:30Z"),
        (("head_before", "fetched_at"), "2026-10-04T03:00:30Z"),
        (("head_after", "fetched_at"), "2026-10-04T02:59:59Z"),
        (("observed_at",), "2026-10-04T03:00:10Z"),
    ],
)
def test_acquisition_period_must_be_ordered(path: tuple[str, ...], stamp: str) -> None:
    value = snapshot()
    target = value["acquisition"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = stamp
    with pytest.raises(EvidenceContractError, match="order"):
        validate(value)


def test_future_observation_beyond_clock_skew_is_rejected() -> None:
    value = snapshot()
    future = NOW + timedelta(seconds=31)
    stamp = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    value["acquisition"]["observed_at"] = stamp
    value["acquisition"]["head_after"]["fetched_at"] = stamp
    with pytest.raises(EvidenceContractError, match="future"):
        validate(value, max_age_seconds=10_000)


def test_observation_within_clock_skew_is_accepted() -> None:
    value = snapshot()
    stamp = (NOW + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    value["acquisition"]["observed_at"] = stamp
    value["acquisition"]["head_after"]["fetched_at"] = stamp
    assert validate(value).observed_at == stamp


def test_record_updated_after_observation_is_inconsistent() -> None:
    record = {"id": 1, "created_at": "2026-10-04T02:00:00Z"}
    record["updated_at"] = "2026-10-04T03:05:00Z"
    with pytest.raises(EvidenceContractError, match="after observed_at"):
        validate(snapshot(issue_comments=[record]))


def test_invalid_record_timestamp_is_a_contract_error() -> None:
    with pytest.raises(EvidenceContractError, match="created_at"):
        validate(snapshot(reviews=[{"id": 1, "submitted_at": "x", "created_at": "x"}]))


def test_record_timestamps_are_normalized_without_mutating_input() -> None:
    original = snapshot(
        issue_comments=[{"id": 1, "created_at": "2026-10-04T11:00:00+09:00"}]
    )
    frozen = copy.deepcopy(original)
    evidence = validate(original)
    assert evidence.state["issue_comments"][0]["created_at"] == "2026-10-04T02:00:00Z"
    assert original == frozen


def test_head_change_during_acquisition_requires_refetch() -> None:
    value = snapshot()
    value["acquisition"]["head_after"]["sha"] = "b" * 40
    with pytest.raises(InsufficientEvidenceError, match="head changed"):
        validate(value)


def test_stale_snapshot_requires_refetch() -> None:
    observed = datetime(2026, 10, 4, 3, 0, 20, tzinfo=UTC)
    limit = observed + timedelta(seconds=DEFAULT_MAX_SNAPSHOT_AGE_SECONDS)
    validate(snapshot(), now=limit)  # exactly at the limit is still fresh
    with pytest.raises(InsufficientEvidenceError, match="stale"):
        validate(snapshot(), now=limit + timedelta(seconds=1))


def test_max_snapshot_age_is_configurable_but_never_unbounded() -> None:
    validate(snapshot(), now=NOW + timedelta(seconds=3000), max_age_seconds=4000)
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(EvidenceContractError, match="max_age"):
            validate(snapshot(), max_age_seconds=bad)


@pytest.mark.parametrize("status", ["partial", "unknown", "truncated", "", None])
@pytest.mark.parametrize("section", ["issue_comments", "reviews", "inline_comments"])
def test_non_complete_section_is_insufficient_evidence(
    section: str, status: Any
) -> None:
    value = snapshot()
    value["completeness"][section] = status
    with pytest.raises(InsufficientEvidenceError, match=section):
        validate(value)


def test_missing_completeness_declaration_is_insufficient_evidence() -> None:
    value = snapshot()
    del value["completeness"]
    with pytest.raises(InsufficientEvidenceError, match="completeness"):
        validate(value)


def test_omitted_completeness_section_is_not_implicitly_complete() -> None:
    value = snapshot()
    del value["completeness"]["reviews"]
    with pytest.raises(InsufficientEvidenceError, match="reviews"):
        validate(value)


def test_non_object_completeness_is_a_contract_error() -> None:
    with pytest.raises(EvidenceContractError, match="completeness"):
        validate(snapshot(completeness="complete"))


@pytest.mark.parametrize("section", ["issue_comments", "reviews", "inline_comments"])
def test_missing_section_is_insufficient_evidence(section: str) -> None:
    value = snapshot()
    del value[section]
    with pytest.raises(InsufficientEvidenceError, match=section):
        validate(value)


def test_missing_acquisition_period_is_insufficient_evidence() -> None:
    value = snapshot()
    del value["acquisition"]
    with pytest.raises(InsufficientEvidenceError, match="acquisition"):
        validate(value)


def test_missing_head_observation_is_insufficient_evidence() -> None:
    value = snapshot()
    del value["acquisition"]["head_before"]
    with pytest.raises(InsufficientEvidenceError, match="head_before"):
        validate(value)


@pytest.mark.parametrize("section", ["issue_comments", "reviews", "inline_comments"])
def test_section_shape_is_validated(section: str) -> None:
    with pytest.raises(EvidenceContractError, match=section):
        validate(snapshot(**{section: {"id": 1}}))
    with pytest.raises(EvidenceContractError, match=section):
        validate(snapshot(**{section: [1]}))


def test_contract_errors_win_over_insufficient_evidence() -> None:
    value = snapshot(pr_number=0)
    value["completeness"]["reviews"] = "partial"
    with pytest.raises(EvidenceContractError):
        validate(value, pr_number=None)


def test_raw_records_are_preserved_beyond_summaries() -> None:
    inline = {
        "id": 9,
        "body": "x",
        "user": {"login": "codex"},
        "pull_request_review_id": 5,
        "path": "a.py",
        "line": 3,
        "commit_id": HEAD,
    }
    evidence = validate(snapshot(inline_comments=[inline]))
    assert evidence.state["inline_comments"] == [inline]


def test_parse_and_normalize_helpers() -> None:
    assert parse_utc("2026-10-04T03:00:00Z", "x") == datetime(
        2026, 10, 4, 3, 0, tzinfo=UTC
    )
    assert normalize_timestamp("2026-10-04T12:00:00+09:00") == "2026-10-04T03:00:00Z"
    assert normalize_timestamp("nope") is None
    assert normalize_timestamp(None) is None
    assert normalize_timestamp("2026-10-04T03:00:00") is None
