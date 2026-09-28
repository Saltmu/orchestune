"""Versioned completion journal and immutable replay data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from orchestune.complete.contracts import (
    CompleteStage,
    CompletionLabelStatus,
    DownstreamPolicyRecord,
    _required_digest,
    _required_text,
    _validate_fixed_outcome,
    can_transition,
)
from orchestune.outcome_record import VALID_RESULTS


def _has_posting_evidence(comment_id: str | None, comment_url: str | None) -> bool:
    return bool(isinstance(comment_id, str) and comment_id.strip()) and bool(
        isinstance(comment_url, str) and comment_url.strip()
    )


@dataclass(frozen=True)
class CompletionJournal:
    """Durable identity and evidence for one completion attempt."""

    issue_number: int
    claim_id: str
    completion_id: str
    result: str
    stage: str
    payload: dict[str, Any] | None = None
    comment_id: str | None = None
    comment_url: str | None = None
    handoff_ready: bool = False


_COMPLETION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CompletionJournalRecord:
    """Versioned completion identity and separately recorded publication proof."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    owner_token_digest: str
    request_fingerprint: str
    result: str
    target_label: str
    outcome_payload: dict[str, Any]
    stage: CompleteStage
    posting_evidence: dict[str, Any] | None = None
    label_evidence: dict[str, Any] | None = None
    prepublication_policy_evidence: dict[str, Any] | None = None
    downstream_policy_records: tuple[DownstreamPolicyRecord, ...] = ()
    schema_version: int = _COMPLETION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_payload()
        self._validate_evidence()
        self._validate_policies()

    def _validate_identity(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.generation_id, "generation_id")
        _required_text(self.completion_id, "completion_id")
        _required_text(self.target_label, "target_label")
        _required_digest(self.owner_token_digest, "owner_token_digest")
        _required_digest(self.request_fingerprint, "request_fingerprint")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if self.result not in VALID_RESULTS:
            raise ValueError(f"invalid completion result: {self.result!r}")

    def _validate_payload(self) -> None:
        if not isinstance(self.outcome_payload, dict):
            raise ValueError("outcome_payload must be an object")
        _validate_fixed_outcome(self.outcome_payload, self.issue_number, self.result)
        if not isinstance(self.stage, CompleteStage):
            try:
                object.__setattr__(self, "stage", CompleteStage(self.stage))
            except ValueError as error:
                raise ValueError(f"unknown completion stage: {self.stage!r}") from error
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported completion journal schema_version: {self.schema_version}"
            )

    def _validate_evidence(self) -> None:
        if self.posting_evidence is not None and not isinstance(
            self.posting_evidence, dict
        ):
            raise ValueError("posting_evidence must be an object or None")
        if self.label_evidence is not None and not isinstance(
            self.label_evidence, dict
        ):
            raise ValueError("label_evidence must be an object or None")
        if self.prepublication_policy_evidence is not None and not isinstance(
            self.prepublication_policy_evidence, dict
        ):
            raise ValueError("prepublication_policy_evidence must be an object or None")
        self._validate_stage_evidence()

    def _validate_policies(self) -> None:
        policy_kinds: set[str] = set()
        for policy in self.downstream_policy_records:
            if not isinstance(policy, DownstreamPolicyRecord):
                raise ValueError(
                    "downstream_policy_records must contain policy records"
                )
            if policy.policy_kind in policy_kinds:
                raise ValueError(
                    "downstream policy kinds must be unique per completion"
                )
            policy_kinds.add(policy.policy_kind)
            if (
                policy.repository_id,
                policy.issue_number,
                policy.generation_id,
                policy.completion_id,
            ) != (
                self.repository_id,
                self.issue_number,
                self.generation_id,
                self.completion_id,
            ):
                raise ValueError("downstream policy identity must match its completion")

    def _validate_stage_evidence(self) -> None:
        if self.stage in {
            CompleteStage.OUTCOME_POSTED,
            CompleteStage.LABEL_CONFIRMED,
            CompleteStage.HANDED_OFF,
        }:
            evidence = self.posting_evidence
            if not evidence or not _has_posting_evidence(
                evidence.get("comment_id"), evidence.get("comment_url")
            ):
                raise ValueError(
                    "outcome-posted stage requires durable comment id and url"
                )
        if self.stage in {CompleteStage.LABEL_CONFIRMED, CompleteStage.HANDED_OFF}:
            evidence = self.label_evidence
            if (
                not evidence
                or evidence.get("status") != CompletionLabelStatus.CONFIRMED.value
            ):
                raise ValueError(
                    "label-confirmed stage requires confirmed label evidence"
                )
            observed = evidence.get("observed_labels")
            if (
                not isinstance(observed, list | tuple)
                or self.target_label not in observed
            ):
                raise ValueError(
                    "confirmed label evidence must include the target label"
                )

    @property
    def journal_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}::{self.generation_id}::{self.completion_id}"

    @property
    def reservation_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}"

    @property
    def receipt_key(self) -> str:
        return self.journal_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "generation_id": self.generation_id,
            "completion_id": self.completion_id,
            "owner_token_digest": self.owner_token_digest,
            "request_fingerprint": self.request_fingerprint,
            "result": self.result,
            "target_label": self.target_label,
            "outcome_payload": self.outcome_payload,
            "stage": self.stage.value,
            "posting_evidence": self.posting_evidence,
            "label_evidence": self.label_evidence,
            "prepublication_policy_evidence": self.prepublication_policy_evidence,
            "downstream_policy_records": {
                record.policy_key: record.to_dict()
                for record in self.downstream_policy_records
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionJournalRecord:
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError("completion_journal schema_version is unsupported")
        parsed = dict(value)
        parsed["stage"] = CompleteStage(parsed["stage"])
        raw_policies = parsed.get("downstream_policy_records", {})
        if not isinstance(raw_policies, dict):
            raise ValueError("downstream_policy_records must be a keyed object")
        policies = tuple(
            DownstreamPolicyRecord.from_dict(record) for record in raw_policies.values()
        )
        if any(
            policy.policy_key != key
            for key, policy in zip(raw_policies, policies, strict=True)
        ):
            raise ValueError(
                "downstream_policy_records key does not match its identity"
            )
        parsed["downstream_policy_records"] = policies
        return cls(**parsed)

    def advance(self, stage: CompleteStage, **evidence: Any) -> CompletionJournalRecord:
        if not can_transition(self.stage, stage):
            raise ValueError(
                f"invalid completion stage transition: {self.stage.value} -> {stage.value}"
            )
        allowed = {
            "posting_evidence",
            "label_evidence",
            "prepublication_policy_evidence",
            "downstream_policy_records",
        }
        if extra := set(evidence) - allowed:
            raise ValueError(f"unknown completion evidence fields: {sorted(extra)}")
        return replace(self, stage=stage, **evidence)


@dataclass(frozen=True)
class CompletionReservation:
    """Issue-level exclusive reservation, independent of worktree existence."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    owner_token_digest: str
    request_fingerprint: str
    result: str
    target_label: str
    outcome_payload: dict[str, Any]
    schema_version: int = _COMPLETION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.generation_id, "generation_id")
        _required_text(self.completion_id, "completion_id")
        _required_text(self.target_label, "target_label")
        _required_digest(self.owner_token_digest, "owner_token_digest")
        _required_digest(self.request_fingerprint, "request_fingerprint")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if self.result not in VALID_RESULTS or not isinstance(
            self.outcome_payload, dict
        ):
            raise ValueError("reservation result or outcome_payload is invalid")
        _validate_fixed_outcome(self.outcome_payload, self.issue_number, self.result)
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported completion reservation schema_version: {self.schema_version}"
            )

    @property
    def reservation_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "generation_id": self.generation_id,
            "completion_id": self.completion_id,
            "owner_token_digest": self.owner_token_digest,
            "request_fingerprint": self.request_fingerprint,
            "result": self.result,
            "target_label": self.target_label,
            "outcome_payload": self.outcome_payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionReservation:
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError("completion_reservations schema_version is unsupported")
        return cls(**value)

    @classmethod
    def from_journal(cls, record: CompletionJournalRecord) -> CompletionReservation:
        return cls(
            repository_id=record.repository_id,
            issue_number=record.issue_number,
            generation_id=record.generation_id,
            completion_id=record.completion_id,
            owner_token_digest=record.owner_token_digest,
            request_fingerprint=record.request_fingerprint,
            result=record.result,
            target_label=record.target_label,
            outcome_payload=record.outcome_payload,
        )


@dataclass(frozen=True)
class CompletionReplayReceipt:
    """Immutable replay data written only after label proof and durable handoff."""

    record: CompletionJournalRecord

    def __post_init__(self) -> None:
        if self.record.stage is not CompleteStage.HANDED_OFF:
            raise ValueError("replay receipt requires a handed_off completion record")

    @property
    def receipt_key(self) -> str:
        return self.record.receipt_key

    def to_dict(self) -> dict[str, Any]:
        return self.record.to_dict()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionReplayReceipt:
        return cls(CompletionJournalRecord.from_dict(value))
