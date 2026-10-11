"""#1296: a promotion verified after the cycle that applied it is reported once."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    IntentStatus,
    TransitionIntent,
)
from orchestune.consistency.repairs.status import COMMAND_TRANSITION_LABEL
from orchestune.consistency.vocabulary import DESIRED_STATUS_LABEL
from orchestune.dispatch.cycle_events import PromotionEvent
from orchestune.dispatch.promotion_reporting import (
    PromotionReportError,
    PromotionReportJournal,
    PromotionReportRecord,
    confirm_promotion_reports,
    confirmed_promotion_intent_ids,
    deliver_promotion_reports,
    is_reportable_promotion,
    promotion_report_journal_path,
)
from orchestune.dispatch.status_repair import (
    execute_status_repair_command,
    reconcile_status_repair_intents,
    status_intent_journal_path,
)
from orchestune.labels import StatusLabel
from tests.conftest import make_issue, make_task
from tests.test_consistency_status_repair import _config, _evidence, _plan

NOW = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
PROMOTION_OPERATION = (
    f"supervisor-status-repair:{COMMAND_TRANSITION_LABEL}:"
    f"{BLOCKED_WITH_RESOLVED_DEPENDENCIES}"
)


def _record(intent_id="status-1-a", issue=1, **changes) -> PromotionReportRecord:
    values = {
        "intent_id": intent_id,
        "issue_number": issue,
        "subtask_id": "task-1",
        "verified_labels": (StatusLabel.QUEUED,),
        "verified_at": NOW.isoformat(),
        "recovered": False,
    }
    values.update(changes)
    return PromotionReportRecord(**values)


# ---- journal ---------------------------------------------------------------------


def test_missing_journal_is_empty(tmp_path):
    assert PromotionReportJournal(tmp_path / "reports.json").load() == ()


def test_register_is_idempotent_and_keeps_the_first_attributes(tmp_path):
    journal = PromotionReportJournal(tmp_path / "reports.json")
    first = journal.register(_record(recovered=False))

    again = journal.register(_record(recovered=True, verified_at="later"))

    assert again == first
    assert journal.load() == (first,)


def test_register_refuses_a_record_that_contradicts_the_intent(tmp_path):
    journal = PromotionReportJournal(tmp_path / "reports.json")
    journal.register(_record(issue=1))

    with pytest.raises(PromotionReportError):
        journal.register(_record(issue=2))
    assert [record.issue_number for record in journal.load()] == [1]


def test_consume_removes_only_the_named_records(tmp_path):
    journal = PromotionReportJournal(tmp_path / "reports.json")
    journal.register(_record("status-1-a"))
    journal.register(_record("status-2-b", issue=2))

    journal.consume({"status-1-a", "unknown"})

    assert [record.intent_id for record in journal.load()] == ["status-2-b"]


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"version": 1, "reports": "x"}', '{"version": 9, "reports": []}'],
)
def test_a_damaged_journal_is_reported_and_left_in_place(tmp_path, content):
    path = tmp_path / "reports.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PromotionReportError):
        PromotionReportJournal(path).load()
    assert path.read_text(encoding="utf-8") == content
    assert list(tmp_path.iterdir()) == [path]


# ---- confirmed log ------------------------------------------------------------------


def _log(path, *entries) -> None:
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )


def test_missing_log_has_no_confirmed_reports(tmp_path):
    assert confirmed_promotion_intent_ids(tmp_path / "events.jsonl") == frozenset()


def test_confirmed_ids_come_from_promotion_events_of_every_line(tmp_path):
    path = tmp_path / "events.jsonl"
    _log(
        path,
        {"promotion_events": [{"issue_number": 1, "subtask_id": "a"}]},
        {"promotion_events": [{"intent_id": "i1"}, {"intent_id": "i2"}]},
        {"selected": [], "completion_events": [{"intent_id": "not-a-promotion"}]},
        {"promotion_events": [{"intent_id": "i3"}]},
    )

    assert confirmed_promotion_intent_ids(path) == {"i1", "i2", "i3"}


@pytest.mark.parametrize("line", ["{broken", "[1, 2]", '{"promotion_events": 3}'])
def test_an_invalid_log_line_is_never_read_as_an_empty_history(tmp_path, line):
    path = tmp_path / "events.jsonl"
    path.write_text(f'{{"promotion_events": []}}\n{line}\n', encoding="utf-8")

    with pytest.raises(PromotionReportError):
        confirmed_promotion_intent_ids(path)


def test_an_unreadable_log_is_reported(tmp_path):
    path = tmp_path / "events.jsonl"
    path.mkdir()

    with pytest.raises(PromotionReportError):
        confirmed_promotion_intent_ids(path)


# ---- what is reportable ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected", "reportable"),
    [
        (PROMOTION_OPERATION, StatusLabel.QUEUED, True),
        (PROMOTION_OPERATION, StatusLabel.DONE, False),
        (PROMOTION_OPERATION, None, False),
        (
            f"supervisor-status-repair:{COMMAND_TRANSITION_LABEL}:{PRIMARY_STATUS_CONFLICT}",
            StatusLabel.QUEUED,
            False,
        ),
        (
            f"supervisor-status-repair:status.add-label:{BLOCKED_WITH_RESOLVED_DEPENDENCIES}",
            StatusLabel.QUEUED,
            False,
        ),
        (
            f"other:{COMMAND_TRANSITION_LABEL}:{BLOCKED_WITH_RESOLVED_DEPENDENCIES}",
            "status:queued",
            False,
        ),
    ],
)
def test_only_a_blocked_to_queued_status_repair_transition_is_reportable(
    operation, expected, reportable
):
    assert is_reportable_promotion(operation, expected) is reportable


# ---- verification registers the report before the intent is verified ----------------------


def _intent(operation=PROMOTION_OPERATION, expected=StatusLabel.QUEUED, issue=1):
    return TransitionIntent(
        intent_id=f"status-{issue}-abc",
        scope=ConsistencyScope.TASK,
        subject_id=str(issue),
        operation=operation,
        created_at=NOW,
        status=IntentStatus.PLANNED,
        expected_changes=(
            DesiredFact(
                name=DESIRED_STATUS_LABEL,
                value=expected,
                scope=ConsistencyScope.TASK,
                subject_id=str(issue),
                reason="test",
            ),
        ),
    )


def _applied_intent(config, **kwargs) -> TransitionIntent:
    journal = IntentJournal(status_intent_journal_path(config))
    intent = journal.plan(_intent(**kwargs))
    journal.mark_applied(intent.intent_id)
    return intent


def _statuses(config) -> dict[str, IntentStatus]:
    return {
        intent.intent_id: intent.status
        for intent in IntentJournal(status_intent_journal_path(config)).load()
    }


def _reports(config):
    return PromotionReportJournal(promotion_report_journal_path(config)).load()


def test_a_later_cycle_verifies_an_applied_promotion_and_keeps_it_unreported(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    in_memory_forge.seed_issue(make_issue(1, labels=(StatusLabel.QUEUED,)))
    intent = _applied_intent(config)

    (verification,) = reconcile_status_repair_intents(config, now=NOW)

    assert verification.intent.intent_id == intent.intent_id
    assert verification.intent.status is IntentStatus.VERIFIED
    assert verification.previous_status is IntentStatus.APPLIED
    assert verification.verified_labels == (StatusLabel.QUEUED,)
    (record,) = _reports(config)
    assert (record.intent_id, record.issue_number) == (intent.intent_id, 1)
    assert record.recovered is True
    assert record.subtask_id is None
    assert record.verified_labels == (StatusLabel.QUEUED,)
    assert _statuses(config) == {intent.intent_id: IntentStatus.VERIFIED}


def test_a_failed_report_registration_leaves_the_intent_unverified(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    in_memory_forge.seed_issue(make_issue(1, labels=(StatusLabel.QUEUED,)))
    intent = _applied_intent(config)

    with patch.object(PromotionReportJournal, "register", side_effect=OSError("disk")):
        assert reconcile_status_repair_intents(config, now=NOW) == ()

    assert _statuses(config) == {intent.intent_id: IntentStatus.APPLIED}
    assert len(reconcile_status_repair_intents(config, now=NOW)) == 1
    assert len(_reports(config)) == 1


def test_a_promotion_that_was_only_planned_is_verified_without_a_report(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    in_memory_forge.seed_issue(make_issue(1, labels=(StatusLabel.QUEUED,)))
    intent = IntentJournal(status_intent_journal_path(config)).plan(_intent())

    (verification,) = reconcile_status_repair_intents(config, now=NOW)

    assert verification.previous_status is IntentStatus.PLANNED
    assert _reports(config) == ()
    assert _statuses(config) == {intent.intent_id: IntentStatus.VERIFIED}


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            f"supervisor-status-repair:{COMMAND_TRANSITION_LABEL}:{PRIMARY_STATUS_CONFLICT}",
            StatusLabel.QUEUED,
        ),
        (PROMOTION_OPERATION, StatusLabel.DONE),
    ],
    ids=["other-finding", "not-a-promotion-target"],
)
def test_other_verified_repairs_are_not_reported(
    tmp_path, in_memory_forge, operation, expected
):
    config = _config(tmp_path, in_memory_forge)
    in_memory_forge.seed_issue(make_issue(1, labels=(expected,)))
    _applied_intent(config, operation=operation, expected=expected)

    assert len(reconcile_status_repair_intents(config, now=NOW)) == 1
    assert _reports(config) == ()


def test_a_dry_run_neither_verifies_nor_registers(tmp_path, in_memory_forge):
    config = _config(tmp_path, in_memory_forge, apply=False)
    in_memory_forge.seed_issue(make_issue(1, labels=(StatusLabel.QUEUED,)))
    intent = _applied_intent(config)

    assert reconcile_status_repair_intents(config, now=NOW) == ()
    assert _statuses(config) == {intent.intent_id: IntentStatus.APPLIED}
    assert not promotion_report_journal_path(config).exists()


def _blocked_task():
    return make_task(
        1,
        subtask_id="task-1",
        status_labels=(StatusLabel.BLOCKED,),
        depends_on=("shadow-supervisor",),
    )


def _promotion_command(task):
    return _plan(
        {task.issue_number: task}, completed_subtask_ids={"shadow-supervisor"}
    )[1][0]


def test_an_immediately_verified_promotion_is_registered_as_not_recovered(
    tmp_path, in_memory_forge
):
    from tests.test_consistency_status_repair import _shadow_dependency

    config = _config(tmp_path, in_memory_forge)
    task = _blocked_task()
    dependency = _shadow_dependency()
    tasks = {task.issue_number: task, dependency.issue_number: dependency}
    in_memory_forge.seed_issue(
        make_issue(
            1,
            labels=task.status_labels,
            subtask_id="task-1",
            depends_on=task.depends_on,
        )
    )
    in_memory_forge.seed_issue(make_issue(709, labels=dependency.status_labels))

    result = execute_status_repair_command(
        _promotion_command(task),
        tasks,
        completion_evidence=_evidence((709,)),
        config=config,
        now=NOW,
    )

    assert result.status.value == "applied"
    (record,) = _reports(config)
    assert (record.issue_number, record.subtask_id) == (1, "task-1")
    assert record.recovered is False
    assert _statuses(config) == {record.intent_id: IntentStatus.VERIFIED}


def test_an_unverified_promotion_registers_nothing_until_a_later_cycle(
    tmp_path, in_memory_forge
):
    from tests.test_consistency_status_repair import _shadow_dependency

    config = _config(tmp_path, in_memory_forge)
    task = _blocked_task()
    dependency = _shadow_dependency()
    tasks = {task.issue_number: task, dependency.issue_number: dependency}
    in_memory_forge.seed_issue(
        make_issue(
            1,
            labels=task.status_labels,
            subtask_id="task-1",
            depends_on=task.depends_on,
        )
    )
    in_memory_forge.seed_issue(make_issue(709, labels=dependency.status_labels))

    with patch(
        "orchestune.dispatch.status_repair._verified_status_labels", return_value=None
    ):
        result = execute_status_repair_command(
            _promotion_command(task),
            tasks,
            completion_evidence=_evidence((709,)),
            config=config,
            now=NOW,
        )

    assert result.status.value == "skipped"
    assert _reports(config) == ()
    (status,) = _statuses(config).values()
    assert status is IntentStatus.APPLIED
    (verification,) = reconcile_status_repair_intents(config, now=NOW)
    assert verification.previous_status is IntentStatus.APPLIED
    assert [record.recovered for record in _reports(config)] == [True]


# ---- delivery and confirmation ---------------------------------------------------------------


def _seed(config, *records) -> PromotionReportJournal:
    journal = PromotionReportJournal(promotion_report_journal_path(config))
    for record in records:
        journal.register(record)
    return journal


def _subtasks(mapping):
    return lambda number: mapping.get(number)


def test_delivery_resolves_the_subtask_and_marks_the_event(tmp_path, in_memory_forge):
    config = _config(tmp_path, in_memory_forge)
    _seed(config, _record("i-1", subtask_id=None, recovered=True))

    events = deliver_promotion_reports(config, _subtasks({1: "task-1"}))

    assert events == [
        PromotionEvent(
            issue_number=1, subtask_id="task-1", recovered=True, intent_id="i-1"
        )
    ]
    assert len(_reports(config)) == 1  # kept until the log append is confirmed


def test_an_unresolvable_subtask_stays_pending_without_an_event(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    _seed(config, _record("i-1", subtask_id=None))

    assert deliver_promotion_reports(config, _subtasks({})) == []
    assert len(_reports(config)) == 1
    assert [
        e.issue_number for e in deliver_promotion_reports(config, _subtasks({1: "t"}))
    ] == [1]


def test_a_report_already_in_the_confirmed_log_is_not_delivered_again(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    _seed(config, _record("i-1"), _record("i-2", issue=2))
    _log(config.events_log_path, {"promotion_events": [{"intent_id": "i-1"}]})

    events = deliver_promotion_reports(config, _subtasks({1: "a", 2: "b"}))

    assert [event.intent_id for event in events] == ["i-2"]
    assert [record.intent_id for record in _reports(config)] == ["i-2"]


def test_an_unreadable_log_withholds_every_report_and_keeps_the_records(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    _seed(config, _record("i-1"))
    config.events_log_path.write_text("{broken\n", encoding="utf-8")

    assert deliver_promotion_reports(config, _subtasks({1: "a"})) == []
    assert len(_reports(config)) == 1


def test_a_damaged_journal_delivers_nothing(tmp_path, in_memory_forge):
    config = _config(tmp_path, in_memory_forge)
    promotion_report_journal_path(config).write_text("{broken", encoding="utf-8")

    assert deliver_promotion_reports(config, _subtasks({1: "a"})) == []


def test_delivery_reads_nothing_when_there_is_no_pending_report(
    tmp_path, in_memory_forge
):
    config = _config(tmp_path, in_memory_forge)
    config.events_log_path.write_text("{broken\n", encoding="utf-8")

    assert deliver_promotion_reports(config, _subtasks({})) == []


def test_confirmation_consumes_the_delivered_records_only(tmp_path, in_memory_forge):
    config = _config(tmp_path, in_memory_forge)
    _seed(config, _record("i-1"), _record("i-2", issue=2))
    delivered = [
        PromotionEvent(issue_number=1, subtask_id="a", intent_id="i-1"),
        PromotionEvent(issue_number=3, subtask_id="c"),
    ]

    confirm_promotion_reports(config, delivered)

    assert [record.intent_id for record in _reports(config)] == ["i-2"]


def test_confirmation_survives_a_damaged_journal(tmp_path, in_memory_forge):
    config = _config(tmp_path, in_memory_forge)
    promotion_report_journal_path(config).write_text("{broken", encoding="utf-8")

    confirm_promotion_reports(
        config, [PromotionEvent(issue_number=1, subtask_id="a", intent_id="i-1")]
    )


# ---- event form -------------------------------------------------------------------------------


def test_the_event_form_is_unchanged_unless_the_new_fields_are_set():
    assert PromotionEvent(issue_number=1, subtask_id="a").to_dict() == {
        "issue_number": 1,
        "subtask_id": "a",
    }
    assert PromotionEvent(
        issue_number=1, subtask_id="a", intent_id="i-1"
    ).to_dict() == {"issue_number": 1, "subtask_id": "a", "intent_id": "i-1"}
    assert PromotionEvent(
        issue_number=1, subtask_id="a", recovered=True, intent_id="i-1"
    ).to_dict() == {
        "issue_number": 1,
        "subtask_id": "a",
        "recovered": True,
        "intent_id": "i-1",
    }


# ---- the whole cycle (the listing-lag reproduction of #1296) -----------------------------------


def _chain_world(tmp_path):
    from tests.dependency_liveness_test_support import (
        CompletionPath,
        LivenessTopology,
        LivenessWorld,
    )

    topology = LivenessTopology(
        (11, 12, 13), ("dep-a",), {11: ("dep-b",), 12: ("dep-c",)}, (11,)
    )
    world = LivenessWorld(tmp_path, topology=topology)
    world.complete(13, CompletionPath.LABEL)
    return world


def _logged_intents(world) -> list[str]:
    path = world.config(apply=True).events_log_path
    if not path.exists():
        return []
    return [
        event["intent_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        for event in json.loads(line)["promotion_events"]
        if "intent_id" in event
    ]


def test_a_promotion_verified_late_under_listing_lag_is_reported_once(tmp_path):
    world = _chain_world(tmp_path)
    world.faults.listing_lag = True

    lagged = world.cycle(apply=True)

    assert lagged.labels_before[12] == {StatusLabel.BLOCKED}
    assert lagged.labels_after[12] == {StatusLabel.QUEUED}
    assert 12 not in lagged.promotion_issue_numbers
    assert lagged.reported_events == ()

    world.faults.listing_lag = False
    later = world.cycle(apply=True)
    (event,) = (e for e in later.reported_events if e.issue_number == 12)
    assert event.recovered is True
    assert event.intent_id is not None
    assert 12 not in later.promotion_issue_numbers
    assert _logged_intents(world) == [event.intent_id]

    for _ in range(2):
        again = world.cycle(apply=True)
        assert all(e.issue_number != 12 for e in again.reported_events)
    assert _logged_intents(world) == [event.intent_id]
    assert (
        PromotionReportJournal(
            world.run_state_path.with_suffix(".promotion-reports.json")
        ).load()
        == ()
    )


def test_a_promotion_verified_in_its_own_cycle_is_reported_in_that_cycle(tmp_path):
    world = _chain_world(tmp_path)

    first = world.cycle(apply=True)

    (event,) = (e for e in first.reported_events if e.issue_number == 12)
    assert event.recovered is False and event.intent_id is not None
    assert 12 in first.promotion_issue_numbers
    assert _logged_intents(world) == [event.intent_id]


def test_a_cycle_interrupted_before_the_log_append_reports_again(tmp_path):
    world = _chain_world(tmp_path)
    log = world.config(apply=True).events_log_path

    with patch(
        "orchestune.dispatch.cycle.append_event_log", side_effect=OSError("disk")
    ):
        interrupted = world.cycle(apply=True)
    assert isinstance(interrupted.error, OSError)
    assert not log.exists()

    retried = world.cycle(apply=True)

    assert [
        e.issue_number for e in retried.reported_events if e.issue_number == 12
    ] == [12]
    assert len(_logged_intents(world)) == 1


def test_a_cycle_interrupted_after_the_log_append_does_not_report_twice(tmp_path):
    world = _chain_world(tmp_path)

    with patch(
        "orchestune.dispatch.cycle_execution.confirm_promotion_reports",
        side_effect=OSError("killed before the cleanup"),
    ):
        interrupted = world.cycle(apply=True)
    assert isinstance(interrupted.error, OSError)
    (intent_id,) = _logged_intents(world)
    journal = PromotionReportJournal(
        world.run_state_path.with_suffix(".promotion-reports.json")
    )
    assert [r.intent_id for r in journal.load()] == [intent_id]

    resumed = world.cycle(apply=True)

    assert all(e.issue_number != 12 for e in resumed.reported_events)
    assert _logged_intents(world) == [intent_id]
    assert journal.load() == ()


def test_a_dry_run_between_the_cycles_changes_nothing_about_the_report(tmp_path):
    world = _chain_world(tmp_path)
    world.faults.listing_lag = True
    world.cycle(apply=True)
    world.faults.listing_lag = False
    intents = world.run_state_path.with_suffix(".status-intents.json")
    before = intents.read_text(encoding="utf-8")

    preview = world.cycle(apply=False)

    assert intents.read_text(encoding="utf-8") == before
    assert not world.run_state_path.with_suffix(".promotion-reports.json").exists()
    assert all(e.recovered is False for e in preview.reported_events)
    assert _logged_intents(world) == []
    later = world.cycle(apply=True)
    assert [e.issue_number for e in later.reported_events if e.recovered] == [12]
