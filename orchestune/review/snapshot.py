"""Validate the MCP review snapshot v1 input contract (no network or filesystem).

`snapshot_version` numbers the *input* contract and is deliberately separate from
the `schema_version` of acquisition results, so a saved result is never mistaken
for an input snapshot. The snapshot is a stored MCP response, not a cryptographic
proof: it never replaces the fresh, independent verification at completion time.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from orchestune.review.markers import normalize_sha
from orchestune.review.tracker_activity import PRECISE_SUFFIX, format_precise_utc

SNAPSHOT_VERSION = 1
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 300
CLOCK_SKEW_SECONDS = 30
SECTIONS = ("issue_comments", "reviews", "inline_comments")

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR_URL = re.compile(
    r"github\.com/(?:repos/)?([^/\s#?]+)/([^/\s#?]+)/(?:pulls?|issues)/(\d+)(?![\d/])",
    re.I,
)
_URL_FIELDS = ("html_url", "url", "issue_url", "pull_request_url")
_TIME_FIELDS = ("created_at", "updated_at", "submitted_at")


class EvidenceContractError(ValueError):
    """Input or contract violation: fix the input, never post or retry (Exit 2)."""


class InsufficientEvidenceError(Exception):
    """Evidence is missing, partial or stale: re-fetch, never fill in (Exit 30)."""


class RoundLimitError(Exception):
    """A requested or posted round exceeds the PR-wide limit (Exit 12)."""


@dataclass(frozen=True)
class SnapshotEvidence:
    repository: str
    pr_number: int
    started_at: str
    observed_at: str
    head_sha: str
    head_fetched_at: str
    state: dict[str, list[dict[str, Any]]]
    completeness: dict[str, str]


def is_snapshot_v1(value: object) -> bool:
    """True when the input declares a snapshot version, whatever its value."""
    return isinstance(value, Mapping) and "snapshot_version" in value


def parse_utc(value: object, field: str) -> datetime:
    """Parse timezone-aware RFC3339 text; naive or malformed input is rejected."""
    if isinstance(value, str) and value == value.strip() and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    raise EvidenceContractError(
        f"{field} must be an RFC3339 timestamp with timezone, got {value!r}"
    )


def _format_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_timestamp(value: object) -> str | None:
    """UTC `...Z` text for a valid timestamp, otherwise None (lenient helper)."""
    try:
        return _format_utc(parse_utc(value, "timestamp"))
    except EvidenceContractError:
        return None


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvidenceContractError(f"{field} must be a positive integer")
    return value


def _check_record_identity(
    record: Mapping[str, Any], section: str, repository: str, pr_number: int
) -> None:
    for field in _URL_FIELDS:
        url = record.get(field)
        match = _PR_URL.search(url) if isinstance(url, str) else None
        if match is None:
            continue
        owner_repo = f"{match[1]}/{match[2]}".lower()
        if owner_repo != repository.lower() or int(match[3]) != pr_number:
            raise EvidenceContractError(
                f"{section} record {record.get('id')!r} belongs to "
                f"{match[1]}/{match[2]}#{match[3]}, not {repository}#{pr_number}"
            )


def _normalize_records(
    items: list[Any], section: str, identity: tuple[str, int], limit: datetime
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise EvidenceContractError(f"{section} must contain objects")
        record = dict(item)
        _check_record_identity(record, section, *identity)
        for field in _TIME_FIELDS:
            # Never trust a caller-supplied sub-second twin; it is derived below.
            record.pop(f"{field}{PRECISE_SUFFIX}", None)
            if record.get(field) in (None, ""):
                continue
            moment = parse_utc(record[field], f"{section}.{field}")
            if moment > limit:
                raise EvidenceContractError(
                    f"{section} record {record.get('id')!r} {field} is after observed_at"
                )
            record[field] = _format_utc(moment)
            if moment.microsecond:
                # `_format_utc` rounds down to the second for string ordering;
                # keep the exact instant for tracker activity comparisons.
                record[f"{field}{PRECISE_SUFFIX}"] = format_precise_utc(moment)
        records.append(record)
    return records


def _head_observation(
    acquisition: Mapping[str, Any], key: str, missing: list[str]
) -> tuple[str, datetime] | None:
    value = acquisition.get(key)
    if value is None:
        missing.append(f"acquisition.{key}")
        return None
    if not isinstance(value, Mapping):
        raise EvidenceContractError(f"acquisition.{key} must be an object")
    sha = normalize_sha(value.get("sha"))
    if sha is None:
        raise EvidenceContractError(
            f"acquisition.{key}.sha must be a 40-hex commit sha"
        )
    return sha, parse_utc(value.get("fetched_at"), f"acquisition.{key}.fetched_at")


def _check_period(times: list[datetime], observed: datetime, now: datetime) -> None:
    if any(earlier > later for earlier, later in zip(times, times[1:], strict=False)):
        raise EvidenceContractError(
            "acquisition times are out of order "
            "(started_at <= head_before <= head_after <= observed_at)"
        )
    if observed > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise EvidenceContractError("acquisition observed_at is in the future")


def _validate_completeness(value: object, missing: list[str]) -> dict[str, str]:
    if value is None:
        missing.append("completeness declaration")
        return {}
    if not isinstance(value, Mapping):
        raise EvidenceContractError("completeness must be an object")
    declared = {section: value.get(section) for section in SECTIONS}
    for section, status in declared.items():
        if status != "complete":
            missing.append(f"completeness.{section} is {status!r}, not 'complete'")
    return {section: str(status) for section, status in declared.items()}


def _read_header(value: Mapping[str, Any], pr_number: int | None) -> tuple[str, int]:
    version = value.get("snapshot_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != SNAPSHOT_VERSION
    ):
        raise EvidenceContractError(
            f"unsupported snapshot_version {version!r}; expected {SNAPSHOT_VERSION}"
        )
    if "acquisition_status" in value:
        raise EvidenceContractError(
            "input looks like an acquisition result, not a review snapshot"
        )
    repository = value.get("repository")
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise EvidenceContractError("repository must be 'owner/repo'")
    snapshot_pr = _positive_int(value.get("pr_number"), "pr_number")
    if pr_number is not None and pr_number != snapshot_pr:
        raise EvidenceContractError(
            f"snapshot PR #{snapshot_pr} differs from requested PR #{pr_number}"
        )
    return repository, snapshot_pr


def _read_acquisition(
    value: Mapping[str, Any], now: datetime, missing: list[str]
) -> tuple[
    datetime, datetime, tuple[str, datetime] | None, tuple[str, datetime] | None
]:
    acquisition = value.get("acquisition")
    if acquisition is None:
        raise InsufficientEvidenceError("snapshot lacks the acquisition period")
    if not isinstance(acquisition, Mapping):
        raise EvidenceContractError("acquisition must be an object")
    started = parse_utc(acquisition.get("started_at"), "acquisition.started_at")
    observed = parse_utc(acquisition.get("observed_at"), "acquisition.observed_at")
    before = _head_observation(acquisition, "head_before", missing)
    after = _head_observation(acquisition, "head_after", missing)
    if before and after:
        _check_period([started, before[1], after[1], observed], observed, now)
    return started, observed, before, after


def _read_sections(
    value: Mapping[str, Any],
    identity: tuple[str, int],
    observed: datetime,
    missing: list[str],
) -> dict[str, list[dict[str, Any]]]:
    limit = observed + timedelta(seconds=CLOCK_SKEW_SECONDS)
    state: dict[str, list[dict[str, Any]]] = {}
    for section in SECTIONS:
        items = value.get(section)
        if items is None:
            missing.append(f"section {section}")
            items = []
        if not isinstance(items, list):
            raise EvidenceContractError(f"{section} must be a list")
        state[section] = _normalize_records(items, section, identity, limit)
    return state


def validate_snapshot(
    value: object,
    *,
    pr_number: int | None,
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> SnapshotEvidence:
    """Validate a v1 snapshot. Contract violations raise before evidence gaps do."""
    if not isinstance(value, Mapping):
        raise EvidenceContractError("review snapshot must be a JSON object")
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int | float)
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
    ):
        raise EvidenceContractError("max_age_seconds must be a positive finite number")
    repository, snapshot_pr = _read_header(value, pr_number)
    missing: list[str] = []
    started, observed, before, after = _read_acquisition(value, now, missing)
    completeness = _validate_completeness(value.get("completeness"), missing)
    state = _read_sections(value, (repository, snapshot_pr), observed, missing)
    if missing:
        raise InsufficientEvidenceError("; ".join(missing))
    assert before is not None and after is not None
    if before[0] != after[0]:
        raise InsufficientEvidenceError(
            "PR head changed during acquisition; re-fetch the snapshot"
        )
    if (now - observed).total_seconds() > max_age_seconds:
        raise InsufficientEvidenceError(
            f"snapshot is stale (observed {_format_utc(observed)}, "
            f"max age {max_age_seconds:g}s); re-fetch it"
        )
    return SnapshotEvidence(
        repository=repository,
        pr_number=snapshot_pr,
        started_at=_format_utc(started),
        observed_at=_format_utc(observed),
        head_sha=after[0],
        head_fetched_at=_format_utc(after[1]),
        state=state,
        completeness=completeness,
    )
