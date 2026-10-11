"""Report a verified status-repair promotion exactly once in the confirmed log (#1296).

A status repair applies ``blocked -> queued`` and then re-reads the Issue to
verify it.  When that read is stale or fails, the intent stays ``APPLIED`` and a
later cycle verifies it; without this module that verification reported
nothing.  Each verified, reportable promotion is therefore first persisted as an
*unreported record* (before its intent becomes ``VERIFIED``), delivered as a
``PromotionEvent`` by an apply cycle, and removed only after the cycle's event
line was appended to ``events.jsonl``.

The guarantee is limited to that confirmed log: an interrupted cycle is
re-delivered, and a record whose id is already in the log is never delivered
again.  Notifications and parent-Issue comments are not exactly-once.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orchestune.consistency.invariants.status import BLOCKED_WITH_RESOLVED_DEPENDENCIES
from orchestune.consistency.repairs.status import COMMAND_TRANSITION_LABEL
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import PromotionEvent
from orchestune.infra.json_state import write_json_atomic
from orchestune.labels import StatusLabel

_LOGGER = logging.getLogger(__name__)

STATUS_REPAIR_OPERATION = "supervisor-status-repair"
_SCHEMA_VERSION = 1


class PromotionReportError(RuntimeError):
    """The report journal or the confirmed log cannot be trusted this cycle."""


@dataclass(frozen=True, slots=True)
class PromotionReportRecord:
    """One verified promotion that the confirmed log does not contain yet."""

    intent_id: str
    issue_number: int
    subtask_id: str | None
    verified_labels: tuple[str, ...]
    verified_at: str
    recovered: bool


def promotion_report_journal_path(config: DispatcherConfig) -> Path:
    """Keep the unreported records beside the run-state file under a stable name."""
    return Path(config.run_state_path).with_suffix(".promotion-reports.json")


def is_reportable_promotion(operation: str, expected_label: str | None) -> bool:
    """A status-repair ``blocked -> queued`` transition for blocked-with-resolved-deps."""
    return (
        expected_label == StatusLabel.QUEUED
        and operation
        == f"{STATUS_REPAIR_OPERATION}:{COMMAND_TRANSITION_LABEL}:"
        f"{BLOCKED_WITH_RESOLVED_DEPENDENCIES}"
    )


def _record_to_json(record: PromotionReportRecord) -> dict[str, object]:
    return {
        "intent_id": record.intent_id,
        "issue_number": record.issue_number,
        "subtask_id": record.subtask_id,
        "verified_labels": list(record.verified_labels),
        "verified_at": record.verified_at,
        "recovered": record.recovered,
    }


def _record_from_json(raw: object) -> PromotionReportRecord:
    if not isinstance(raw, dict):
        raise PromotionReportError("promotion report record is not an object")
    labels = raw.get("verified_labels")
    subtask = raw.get("subtask_id")
    if (
        not isinstance(raw.get("intent_id"), str)
        or not isinstance(raw.get("issue_number"), int)
        or isinstance(raw.get("issue_number"), bool)
        or not (subtask is None or isinstance(subtask, str))
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or not isinstance(raw.get("verified_at"), str)
        or not isinstance(raw.get("recovered"), bool)
    ):
        raise PromotionReportError("promotion report record has an invalid field")
    return PromotionReportRecord(
        intent_id=raw["intent_id"],
        issue_number=raw["issue_number"],
        subtask_id=subtask,
        verified_labels=tuple(labels),
        verified_at=raw["verified_at"],
        recovered=raw["recovered"],
    )


class PromotionReportJournal:
    """Atomic journal of unreported promotions.

    A damaged or unreadable file is never treated as empty or quarantined: the
    records it may hold are the only proof of an unreported promotion.  Callers
    hold the run-state lock, as for the intent journal.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[PromotionReportRecord, ...]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as exc:
            raise PromotionReportError(f"cannot read {self.path.name}: {exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PromotionReportError(f"{self.path.name} is not JSON: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _SCHEMA_VERSION
            or not isinstance(raw.get("reports"), list)
        ):
            raise PromotionReportError(f"{self.path.name} has an unknown layout")
        return tuple(_record_from_json(item) for item in raw["reports"])

    def _save(self, records: Iterable[PromotionReportRecord]) -> None:
        ordered = sorted(records, key=lambda record: record.intent_id)
        write_json_atomic(
            self.path,
            {
                "version": _SCHEMA_VERSION,
                "reports": [_record_to_json(record) for record in ordered],
            },
        )

    def register(self, record: PromotionReportRecord) -> PromotionReportRecord:
        """Persist ``record``; a second registration of an intent keeps the first."""
        current = list(self.load())
        for existing in current:
            if existing.intent_id != record.intent_id:
                continue
            if existing.issue_number != record.issue_number:
                raise PromotionReportError(
                    f"conflicting report for intent {record.intent_id}"
                )
            return existing
        current.append(record)
        self._save(current)
        return record

    def consume(self, intent_ids: Iterable[str]) -> None:
        ids = set(intent_ids)
        records = self.load()
        kept = [record for record in records if record.intent_id not in ids]
        if len(kept) != len(records):
            self._save(kept)


def confirmed_promotion_intent_ids(events_log_path: str | Path) -> frozenset[str]:
    """Intent ids of promotion events already appended to the confirmed log.

    A log that does not exist yet is empty.  Any unreadable content is an error:
    treating it as an empty history would report the same promotion twice.
    """
    path = Path(events_log_path)
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                entry = json.loads(line)
                events = (
                    entry.get("promotion_events", [])
                    if isinstance(entry, dict)
                    else None
                )
                if not isinstance(events, list):
                    raise PromotionReportError(
                        f"{path.name} line {number} has no promotion event list"
                    )
                ids.update(
                    event["intent_id"]
                    for event in events
                    if isinstance(event, dict)
                    and isinstance(event.get("intent_id"), str)
                )
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionReportError(f"cannot read {path.name}: {exc}") from exc
    return frozenset(ids)


def deliver_promotion_reports(
    config: DispatcherConfig, resolve_subtask: Callable[[int], str | None]
) -> list[PromotionEvent]:
    """Events for the unreported records the confirmed log does not contain.

    Records already in the log (appended before an interrupted cleanup) are
    dropped.  A record whose Issue has no resolvable subtask stays for a later
    cycle.  Any error withholds every report and keeps every record.
    """
    journal = PromotionReportJournal(promotion_report_journal_path(config))
    try:
        records = journal.load()
        if not records:
            return []
        reported = confirmed_promotion_intent_ids(config.events_log_path)
        journal.consume(
            record.intent_id for record in records if record.intent_id in reported
        )
    except (PromotionReportError, OSError) as exc:
        _LOGGER.warning("promotion reports withheld this cycle: %s", exc)
        return []
    events: list[PromotionEvent] = []
    for record in sorted(records, key=lambda r: (r.verified_at, r.intent_id)):
        if record.intent_id in reported:
            continue
        subtask = record.subtask_id or resolve_subtask(record.issue_number)
        if subtask is None:
            _LOGGER.warning(
                "promotion report %s waits: issue #%s has no resolvable task",
                record.intent_id,
                record.issue_number,
            )
            continue
        events.append(
            PromotionEvent(
                issue_number=record.issue_number,
                subtask_id=subtask,
                recovered=record.recovered,
                intent_id=record.intent_id,
            )
        )
    return events


class _TaskLookup(Protocol):
    def task(self, issue_number: int) -> Any: ...


def deliver_cycle_promotion_reports(
    ctx: _TaskLookup, config: DispatcherConfig
) -> list[PromotionEvent]:
    """The cycle's verified-but-unreported promotions (apply only; #1296)."""
    if not config.apply:
        return []

    def subtask_of(issue_number: int) -> str | None:
        task = ctx.task(issue_number)
        return task.subtask_id if task is not None and task.subtask_id else None

    return deliver_promotion_reports(config, subtask_of)


def confirm_promotion_reports(
    config: DispatcherConfig, events: Sequence[PromotionEvent]
) -> None:
    """Drop the delivered records once their event line is in the confirmed log."""
    ids = [event.intent_id for event in events if event.intent_id is not None]
    if not ids:
        return
    try:
        PromotionReportJournal(promotion_report_journal_path(config)).consume(ids)
    except (PromotionReportError, OSError) as exc:
        # The next cycle's log check removes the record without re-reporting it.
        _LOGGER.warning("promotion reports kept after the log append: %s", exc)


__all__ = [
    "PromotionReportError",
    "PromotionReportJournal",
    "PromotionReportRecord",
    "confirm_promotion_reports",
    "confirmed_promotion_intent_ids",
    "deliver_cycle_promotion_reports",
    "deliver_promotion_reports",
    "is_reportable_promotion",
    "promotion_report_journal_path",
]
