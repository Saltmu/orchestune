from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    IntentStatus,
    TransitionIntent,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _intent(
    intent_id: str = "launch-702",
    *,
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
    operation: str = "launch-task",
) -> TransitionIntent:
    return TransitionIntent(
        intent_id=intent_id,
        scope=ConsistencyScope.TASK,
        subject_id="702",
        operation=operation,
        created_at=created_at,
        expires_at=expires_at,
        expected_changes=(
            DesiredFact(
                name="task.status_label",
                value="status:in-progress",
                scope=ConsistencyScope.TASK,
                subject_id="702",
                reason="task launch",
            ),
            DesiredFact(
                name="task.run_state",
                value=("active", ("branch", "codex/issue-702")),
                scope=ConsistencyScope.TASK,
                subject_id="702",
                reason="task launch",
            ),
            DesiredFact(
                name="task.pull_request_state",
                value="open",
                scope=ConsistencyScope.TASK,
                subject_id="702",
                reason="task launch",
            ),
        ),
    )


def test_plan_persists_every_external_change_before_mutation_can_start(
    tmp_path,
) -> None:
    path = tmp_path / "intent_journal.json"
    journal = IntentJournal(path)

    planned = journal.plan(_intent())
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert planned.status is IntentStatus.PLANNED
    assert raw["version"] == 1
    assert raw["intents"][0]["intent_id"] == "launch-702"
    assert [change["name"] for change in raw["intents"][0]["expected_changes"]] == [
        "task.status_label",
        "task.run_state",
        "task.pull_request_state",
    ]


def test_round_trip_preserves_timestamps_scopes_and_recursive_fact_values(
    tmp_path,
) -> None:
    path = tmp_path / "intent_journal.json"
    original = _intent(expires_at=NOW + timedelta(hours=1))

    IntentJournal(path).plan(original)
    loaded = IntentJournal(path).load()

    assert loaded == (original,)
    assert loaded[0].expected_changes[1].value == (
        "active",
        ("branch", "codex/issue-702"),
    )


def test_restarting_and_replanning_same_intent_is_idempotent(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    intent = _intent()
    first_journal = IntentJournal(path)
    first_journal.plan(intent)
    applied = first_journal.mark_applied(intent.intent_id)

    restarted = IntentJournal(path)
    replayed = restarted.plan(intent)

    assert replayed == applied
    assert replayed.status is IntentStatus.APPLIED
    assert restarted.load() == (applied,)


def test_reusing_intent_id_for_different_transition_is_rejected(tmp_path) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    journal.plan(_intent())

    with pytest.raises(ValueError, match="conflicting intent_id"):
        journal.plan(_intent(operation="close-task"))


def test_lifecycle_transitions_are_persisted_and_terminal(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    journal = IntentJournal(path)
    journal.plan(_intent())

    with pytest.raises(ValueError, match="planned.*verified"):
        journal.mark_verified("launch-702")

    applied = journal.mark_applied("launch-702")
    verified = journal.mark_verified("launch-702")

    assert applied.status is IntentStatus.APPLIED
    assert verified.status is IntentStatus.VERIFIED
    assert journal.mark_verified("launch-702") == verified
    with pytest.raises(ValueError, match="verified.*failed"):
        journal.mark_failed("launch-702", diagnostics=("too late",))
    assert IntentJournal(path).load() == (verified,)


def test_failed_intent_retains_diagnostics_and_is_not_pending(tmp_path) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    journal.plan(_intent())

    failed = journal.mark_failed(
        "launch-702",
        diagnostics=("GitHub label mutation failed", "retry exhausted"),
    )

    assert failed.status is IntentStatus.FAILED
    assert failed.diagnostics == (
        "GitHub label mutation failed",
        "retry exhausted",
    )
    assert journal.pending(now=NOW) == ()
    assert journal.load() == (failed,)


def test_expire_overdue_marks_planned_and_applied_intents_with_evidence(
    tmp_path,
) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    created_at = NOW - timedelta(hours=1)
    journal.plan(_intent("planned", created_at=created_at, expires_at=NOW))
    journal.plan(
        _intent(
            "applied",
            created_at=created_at,
            expires_at=NOW - timedelta(seconds=1),
        )
    )
    journal.mark_applied("applied")
    journal.plan(_intent("future", expires_at=NOW + timedelta(seconds=1)))

    expired = journal.expire_overdue(now=NOW)

    assert [intent.intent_id for intent in expired] == ["applied", "planned"]
    assert all(intent.status is IntentStatus.EXPIRED for intent in expired)
    assert all("expired at" in intent.diagnostics[-1] for intent in expired)
    assert [intent.intent_id for intent in journal.pending(now=NOW)] == ["future"]


def test_pending_excludes_overdue_record_even_before_expiry_is_persisted(
    tmp_path,
) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    journal.plan(
        _intent(
            created_at=NOW - timedelta(hours=1),
            expires_at=NOW - timedelta(microseconds=1),
        )
    )

    assert journal.pending(now=NOW) == ()
    assert journal.load()[0].status is IntentStatus.PLANNED


def test_atomic_write_failure_leaves_last_complete_lifecycle_state(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    journal = IntentJournal(path)
    planned = journal.plan(_intent())

    with patch(
        "orchestune.consistency.intents.write_json_atomic",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            journal.mark_applied("launch-702")

    assert IntentJournal(path).load() == (planned,)


def test_each_crash_point_can_resume_without_duplicate_side_effect_basis(
    tmp_path,
) -> None:
    path = tmp_path / "intent_journal.json"
    intent = _intent()

    # Before the first external change and between partial external changes, the
    # complete planned target survives. Replanning after restart returns it.
    IntentJournal(path).plan(intent)
    before_change = IntentJournal(path).plan(intent)
    between_changes = IntentJournal(path).load()[0]
    assert before_change == between_changes == intent

    # Once all changes have been applied, restart retains the applied marker.
    IntentJournal(path).mark_applied(intent.intent_id)
    after_changes = IntentJournal(path).plan(intent)
    assert after_changes.status is IntentStatus.APPLIED

    # Verification is terminal and remains available for diagnosis/deduplication.
    IntentJournal(path).mark_verified(intent.intent_id)
    after_verification = IntentJournal(path).plan(intent)
    assert after_verification.status is IntentStatus.VERIFIED
    assert len(IntentJournal(path).load()) == 1


def test_corrupted_json_is_quarantined_and_recovers_empty(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    path.write_text("{not json", encoding="utf-8")

    assert IntentJournal(path).load() == ()
    assert not path.exists()
    assert len(list(tmp_path.glob("intent_journal.json.corrupt.*"))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 2, "intents": []},
        {"version": 1, "intents": "not-a-list"},
        {"version": 1, "intents": [{"intent_id": "incomplete"}]},
    ],
)
def test_semantically_invalid_journal_fails_closed(tmp_path, payload) -> None:
    path = tmp_path / "intent_journal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="intent journal"):
        IntentJournal(path).load()


def test_missing_intent_and_naive_timestamps_are_rejected(tmp_path) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    with pytest.raises(KeyError, match="missing"):
        journal.mark_applied("missing")
    with pytest.raises(ValueError, match="timezone-aware"):
        journal.plan(_intent(created_at=datetime(2026, 8, 28, 12, 0)))


def test_invalid_new_intent_fields_and_lifecycle_requests_are_rejected(
    tmp_path,
) -> None:
    journal = IntentJournal(tmp_path / "intent_journal.json")
    with pytest.raises(ValueError, match="planned status"):
        journal.plan(replace(_intent(), status=IntentStatus.APPLIED))
    with pytest.raises(ValueError, match="intent_id"):
        journal.plan(replace(_intent(), intent_id=""))
    with pytest.raises(ValueError, match="operation"):
        journal.plan(replace(_intent(), operation=""))
    with pytest.raises(ValueError, match="later than"):
        journal.plan(_intent(expires_at=NOW))
    journal.plan(_intent())
    with pytest.raises(ValueError, match="requires diagnostics"):
        journal.mark_failed("launch-702", diagnostics=())
    assert journal.mark_applied("launch-702").status is IntentStatus.APPLIED
    assert journal.mark_applied("launch-702").status is IntentStatus.APPLIED


def _valid_payload() -> dict:
    return {
        "version": 1,
        "intents": [
            {
                "intent_id": "launch-702",
                "scope": "task",
                "operation": "launch-task",
                "created_at": NOW.isoformat(),
                "subject_id": "702",
                "status": "planned",
                "expires_at": None,
                "expected_changes": [
                    {
                        "name": "task.status_label",
                        "value": "status:in-progress",
                        "scope": "task",
                        "subject_id": "702",
                        "reason": "task launch",
                    }
                ],
                "diagnostics": [],
            }
        ],
    }


def test_invalid_entry_fields_fail_closed_with_diagnostics(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    mutations = (
        lambda entry: entry.update(intent_id=""),
        lambda entry: entry.update(operation=""),
        lambda entry: entry.update(subject_id=702),
        lambda entry: entry.update(expected_changes="bad"),
        lambda entry: entry.update(status=1),
        lambda entry: entry.update(status="unknown"),
        lambda entry: entry.update(scope="unknown"),
        lambda entry: entry.update(created_at=1),
        lambda entry: entry.update(created_at="not-a-date"),
        lambda entry: entry.update(created_at="2026-08-28T12:00:00"),
        lambda entry: entry.update(expires_at=1),
        lambda entry: entry.update(diagnostics="bad"),
    )
    for mutate in mutations:
        payload = _valid_payload()
        mutate(payload["intents"][0])
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid intent journal"):
            IntentJournal(path).load()


def test_invalid_expected_change_fields_fail_closed(tmp_path) -> None:
    path = tmp_path / "intent_journal.json"
    mutations = (
        lambda changes: changes.__setitem__(0, "bad"),
        lambda changes: changes[0].update(name=""),
        lambda changes: changes[0].update(subject_id=702),
        lambda changes: changes[0].update(reason=1),
        lambda changes: changes[0].pop("value"),
        lambda changes: changes[0].update(value={"unsupported": True}),
        lambda changes: changes[0].update(value=math.inf),
        lambda changes: changes[0].update(scope="unknown"),
    )
    for mutate in mutations:
        payload = _valid_payload()
        mutate(payload["intents"][0]["expected_changes"])
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid intent journal"):
            IntentJournal(path).load()


def test_duplicate_persisted_ids_and_naive_clock_requests_are_rejected(
    tmp_path,
) -> None:
    path = tmp_path / "intent_journal.json"
    payload = _valid_payload()
    payload["intents"].append(dict(payload["intents"][0]))
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate intent_id"):
        IntentJournal(path).load()

    path.unlink()
    journal = IntentJournal(path)
    with pytest.raises(ValueError, match="timezone-aware"):
        journal.pending(now=datetime(2026, 8, 28, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        journal.expire_overdue(now=datetime(2026, 8, 28, 12, 0))
    assert journal.expire_overdue(now=NOW) == ()
