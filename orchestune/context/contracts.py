"""shared-artifact-v1 values derived from existing records.

GitHub Issue and PR text stays the source of record. These values do not
replace ``OutcomeRecord`` and are not written back to GitHub.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from orchestune.outcome_record import (
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeLookupState,
)

SCHEMA_VERSION = "shared-artifact-v1"
TASK_KIND_UNSPECIFIED = "unspecified"
RECORDED_COMPLETION = "recorded_completion"
REFERENCE_HISTORY = "reference_history"
STATEMENTS_MARKER = "<!-- orchestune:artifact-statements -->"
CORRECTION_MARKER = "<!-- orchestune:artifact-correction -->"


class ArtifactStaleStatus(StrEnum):
    """Stale judgment copied onto an artifact. Unevaluated is ``unknown``."""

    CANDIDATE_STALE = "candidate_stale"
    CONFIRMED_STALE = "confirmed_stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SharedArtifactKey:
    """Identity of one extracted snapshot. An edit changes the digest."""

    repository: str
    issue_number: int
    source_comment_id: str
    source_revision_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceRefs:
    """Pointers copied from the source. Missing URLs stay missing."""

    pr_url: str | None = None
    comment_url: str | None = None
    changed_files: tuple[str, ...] | None = None
    read_evidence_digest: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """CI and review fields already stored on the outcome."""

    ci: str | None = None
    review_verdict: str | None = None
    review_rounds: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedArtifactV1:
    """One derived record. ``indexed_at`` stays missing until a later ingest."""

    schema_version: str
    repository: str
    issue_number: int
    source_comment_id: str
    source_revision_digest: str
    parent_issue: int | None
    subtask_id: str | None
    claim_id: str | None
    attempt: int | None
    completion_id: str | None
    base_sha: str | None
    target_commit: str | None
    task_kind: str
    result: str
    summary: str | None
    assumptions: tuple[str, ...] | None
    evidence_refs: EvidenceRefs
    verification_summary: VerificationSummary
    supersedes: SharedArtifactKey | None
    superseded_by: tuple[SharedArtifactKey, ...]
    stale_status: ArtifactStaleStatus
    recorded_at: str | None
    indexed_at: str | None
    source_url: str | None
    plan_revision_digest: str | None
    depends_on: tuple[str, ...] | None
    footprint: tuple[str, ...] | None
    symbols: tuple[str, ...] | None
    dependency_issue_numbers: tuple[int | None, ...] | None

    def key(self) -> SharedArtifactKey:
        """Return the extraction identity of this record."""
        return SharedArtifactKey(
            self.repository,
            self.issue_number,
            self.source_comment_id,
            self.source_revision_digest,
        )

    def proves_current_dependencies(self) -> bool:
        """Return whether this record proves the current dependency contract.

        ``done`` is not that proof. The method is always false.
        """
        return False

    def history_role(self) -> str:
        """Separate a recorded completion from blocked reference history."""
        if self.result == RESULT_BLOCKED:
            return REFERENCE_HISTORY
        if self.result in {RESULT_DONE, RESULT_NOT_NEEDED}:
            return RECORDED_COMPLETION
        return "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanSubtaskMetadata:
    """One subtask from a passed plan revision. Not read from live Issue text."""

    subtask_id: str
    issue_number: int | None
    depends_on: tuple[str, ...] = ()
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    task_kind: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanMetadataSnapshot:
    """Plan metadata at one revision. The digest is the caller's revision id."""

    revision_digest: str
    parent_issue: int | None
    subtasks: tuple[PlanSubtaskMetadata, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleJudgment:
    """A passed detection or validation result for one reader.

    ``report_digest`` is the report hash. ``evidence_digest`` names the reader
    evidence that report covers. Those hashes are different payloads.
    """

    target_commit: str
    report_digest: str
    evidence_digest: str
    status: ArtifactStaleStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionRequest:
    """Passed sources. ``fetch_complete`` false is a failed or partial read."""

    repository: str
    issue_number: int
    fetch_complete: bool
    comments: tuple[Mapping[str, Any], ...] | None = None
    plan: PlanMetadataSnapshot | None = None
    stale_judgments: tuple[StaleJudgment, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Lookup state plus the records derived from one complete snapshot."""

    state: OutcomeLookupState
    artifacts: tuple[SharedArtifactV1, ...]


LAUNCH_CONTEXT_SCHEMA = "launch-context-v1"
CONTEXT_READY = "ready"
CONTEXT_OVER_BUDGET = "over_budget"
CONTEXT_UNAVAILABLE = "unavailable"
TIER2_READY = "ready"
TIER2_REDUCED = "reduced"
TIER2_UNAVAILABLE = "unavailable"
TIER2_EMPTY = "empty"
REDUCTION_TIER1_EXCEEDS_BUDGET = "tier1_exceeds_budget"
REDUCTION_TIER2_TAIL_DROPPED = "tier2_tail_dropped"


@dataclass(frozen=True, slots=True, kw_only=True)
class DependencyContext:
    """Confirmed outcome of one direct dependency, or an unresolved result."""

    issue_number: int | None = None
    subtask_id: str | None = None
    result: str | None = None
    summary: str | None = None
    pr: int | None = None
    reason: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    claim_id: str | None = None
    completion_id: str | None = None
    attempt: int | None = None
    ci: str | None = None
    review_verdict: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Tier1Context:
    """Required launch facts. Rank and byte budget never drop these fields."""

    purpose: str
    acceptance: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    parent_purpose: str
    parent_acceptance: str
    dependencies: tuple[DependencyContext, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Tier2Hit:
    """One ranked artifact kept inside the launch budget."""

    issue_number: int
    title: str
    summary: str | None
    source_url: str | None = None
    pr_url: str | None = None
    comment_url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchContext:
    """Tiered launch payload. ``size_bytes`` is measured without itself."""

    schema_version: str
    context_status: str
    tier2_status: str
    reduction_reason: str | None
    tier1: Tier1Context | None
    tier2: tuple[Tier2Hit, ...]
    tier3_urls: tuple[str, ...]
    size_bytes: int
    max_bytes: int
