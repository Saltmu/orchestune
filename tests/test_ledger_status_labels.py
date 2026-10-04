from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orchestune.forge import Forge
from orchestune.labels import StatusLabel
from orchestune.ledger.status_labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.ledger.status_machine import (
    LABEL_ROLES,
    LabelRole,
    lifecycle_labels,
)


class TestTransitionStatusLabel:
    def test_adds_new_label_before_removing_old_ones(self):
        # #381: 途中で例外が起きてもIssueが必ずいずれかのラベルを持ち続ける
        # ことを保証するため、addが必ずremoveより先に呼ばれなければならない。
        forge = MagicMock()
        call_order: list[str] = []
        forge.add_label.side_effect = lambda *a, **k: call_order.append("add")
        forge.remove_label.side_effect = lambda *a, **k: call_order.append("remove")

        transition_status_label(forge, 1, "status:done", ("status:in-progress",))

        assert call_order == ["add", "remove"]
        forge.add_label.assert_called_once_with(1, "status:done")
        forge.remove_label.assert_called_once_with(1, "status:in-progress")

    def test_removes_every_old_label_provided(self):
        forge = MagicMock()

        transition_status_label(
            forge, 1, "status:in-progress", ("status:queued", "status:blocked")
        )

        forge.remove_label.assert_any_call(1, "status:queued")
        forge.remove_label.assert_any_call(1, "status:blocked")
        assert forge.remove_label.call_count == 2

    def test_does_not_remove_old_label_matching_the_new_label(self):
        # 起動失敗の再試行等で、旧ラベルと新ラベルが同名になりうる
        # （例: 既にstatus:blockedだったタスクが再び起動失敗しstatus:blocked
        # を付与し直す）。この場合、addで付与した直後に自分自身を消して
        # しまってはならない。
        forge = MagicMock()

        transition_status_label(
            forge, 1, "status:blocked", ("status:queued", "status:blocked")
        )

        forge.add_label.assert_called_once_with(1, "status:blocked")
        forge.remove_label.assert_called_once_with(1, "status:queued")

    def test_empty_old_labels_only_adds(self):
        forge = MagicMock()

        transition_status_label(forge, 1, "status:in-progress", ())

        forge.add_label.assert_called_once_with(1, "status:in-progress")
        forge.remove_label.assert_not_called()


class TestCompletionTransition:
    def run(
        self,
        labels,
        *,
        add_error=False,
        remove_error=False,
        lost=False,
        generation=lambda: True,
        reads=None,
    ):
        from orchestune.complete.status_labels import transition_completion_status_label

        live = set(labels)
        forge = MagicMock()
        operations = []

        def add(number, label):
            operations.append(("add", label))
            if not add_error or lost:
                live.add(label)
            if add_error:
                raise RuntimeError("lost add")

        def remove(number, label):
            operations.append(("remove", label))
            if not remove_error or lost:
                live.discard(label)
            if remove_error:
                raise RuntimeError("lost remove")

        forge.add_label.side_effect = add
        forge.remove_label.side_effect = remove
        forge.get_issue_labels.side_effect = reads or (lambda n: tuple(sorted(live)))
        result = transition_completion_status_label(
            forge, 1, "status:done", generation_matches=generation
        )
        return result, operations, live

    def test_confirmed_order_and_preservation(self):
        result, ops, live = self.run(
            [
                "status:in-progress",
                "status:queued",
                "status:blocked",
                "status:force-serial",
                "feature",
            ]
        )
        assert result.confirmed
        assert ops[0] == ("add", "status:done")
        assert live == {"status:done", "status:force-serial", "feature"}

    def test_existing_target_is_not_added_or_removed(self):
        result, ops, _ = self.run(["status:done", "status:queued"])
        assert result.confirmed
        assert ops == [("remove", "status:queued")]
        assert self.run(["status:done"])[1] == []

    def test_add_failure_does_not_remove(self):
        result, ops, _ = self.run(["status:queued"], add_error=True)
        assert result.status.value == "add_failed"
        assert result.failed_operation == "add:status:done"
        assert ops == [("add", "status:done")]

    def test_partial_cleanup_and_replay(self):
        result, ops, live = self.run(["status:queued"], remove_error=True)
        assert result.status.value == "cleanup_incomplete"
        assert result.failed_operation == "remove:status:queued"
        replay, ops, _ = self.run(live)
        assert replay.confirmed
        assert ops == [("remove", "status:queued")]

    def test_response_loss_recovers_from_live_state(self):
        for add, remove in [(True, False), (False, True), (True, True)]:
            assert self.run(
                ["status:queued"], add_error=add, remove_error=remove, lost=True
            )[0].confirmed

    def test_protected_unknown_and_other_terminal_labels_conflict(self):
        for label in [
            "status:blocked-human-review",
            "status:manual-merge-required",
            "status:external-lock",
            "status:blocked-recompute",
            "status:not-needed",
            "status:future",
        ]:
            result, ops, live = self.run([label, "status:queued"])
            assert result.status.value == "conflict"
            assert ops == []
            assert label in live

    def test_generation_mismatch_and_failure(self):
        result, ops, _ = self.run(["status:queued"], generation=lambda: False)
        assert result.status.value == "conflict"
        assert ops == []

    def test_final_get_failure_is_unknown(self):
        result, _, _ = self.run(
            ["status:done"], reads=[("status:done",), RuntimeError("get")]
        )
        assert result.status.value == "unknown"

    def test_protection_appearing_after_add_prevents_cleanup(self):
        result, ops, _ = self.run(
            ["status:queued"],
            reads=[
                ("status:queued",),
                ("status:done", "status:queued", "status:external-lock"),
            ],
        )
        assert result.status.value == "conflict"
        assert ops == [("add", "status:done")]

    def test_initial_get_failure_is_unknown_without_writes(self):
        result, ops, _ = self.run([], reads=[RuntimeError("unavailable")])
        assert result.status.value == "unknown"
        assert result.failed_operation == "get"
        assert ops == []

    def test_generation_callback_failure_is_unknown(self):
        def unavailable():
            raise RuntimeError("cannot reconcile reservation")

        result, ops, _ = self.run(["status:queued"], generation=unavailable)
        assert result.status.value == "unknown"
        assert ops == []

    def test_generation_changes_after_add(self):
        generations = iter([True, False])
        result, ops, _ = self.run(
            ["status:queued"], generation=lambda: next(generations)
        )
        assert result.status.value == "conflict"
        assert ops == [("add", "status:done")]

    def test_missing_target_before_cleanup_does_not_remove(self):
        result, ops, _ = self.run(
            ["status:queued"],
            reads=[("status:queued", "status:done"), ("status:queued",)],
        )
        assert result.status.value == "unknown"
        assert ops == []

    def test_final_observation_detects_new_old_label(self):
        result, ops, _ = self.run(
            ["status:done"], reads=[("status:done",), ("status:done", "status:queued")]
        )
        assert result.status.value == "cleanup_incomplete"
        assert ops == []

    def test_each_completion_target(self):
        from orchestune.complete.status_labels import transition_completion_status_label

        for target in ("status:done", "status:blocked", "status:not-needed"):
            live = {target, "status:queued"}
            forge = MagicMock()
            forge.get_issue_labels.side_effect = lambda n, live=live: tuple(live)
            forge.remove_label.side_effect = lambda n, label, live=live: live.discard(
                label
            )
            result = transition_completion_status_label(
                forge, 1, target, generation_matches=lambda: True
            )
            assert result.confirmed
            assert live == {target}
            forge.add_label.assert_not_called()

    def test_invalid_target_does_not_touch_forge(self):
        import pytest

        from orchestune.complete.status_labels import transition_completion_status_label

        forge = MagicMock()
        with pytest.raises(ValueError):
            transition_completion_status_label(
                forge, 1, "status:queued", generation_matches=lambda: True
            )
        assert forge.mock_calls == []

    def test_legacy_callback_runs_before_cleanup(self):
        forge = MagicMock()
        operations = []
        forge.add_label.side_effect = lambda *args: operations.append("add")
        forge.remove_label.side_effect = lambda *args: operations.append("remove")
        transition_status_label(
            forge,
            1,
            "status:done",
            ["status:queued"],
            on_label_added=lambda: operations.append("callback"),
        )
        assert operations == ["add", "callback", "remove"]


def _legacy_transition_status_label(
    forge: Any,
    issue_number: int | str,
    new_label: str,
    old_labels: Iterable[str],
    on_label_added: Callable[[], None] | None = None,
) -> None:
    """The adapter body as it was before #1217; the behavioural reference."""
    forge.add_label(issue_number, new_label)
    if on_label_added is not None:
        on_label_added()
    for old_label in old_labels:
        if old_label != new_label:
            forge.remove_label(issue_number, old_label)


class _Boom(Exception):
    """Marker raised by injected failures."""


class _RecordingForge:
    """Records every operation in one shared history and injects failures."""

    def __init__(
        self,
        history: list[tuple[Any, ...]],
        fail_add: bool = False,
        fail_remove: str | None = None,
    ) -> None:
        self.history = history
        self.fail_add = fail_add
        self.fail_remove = fail_remove

    def add_label(self, issue_number: int | str, label: str) -> None:
        self.history.append(("add", issue_number, label))
        if self.fail_add:
            raise _Boom("add")

    def remove_label(self, issue_number: int | str, label: str) -> None:
        self.history.append(("remove", issue_number, label))
        if label == self.fail_remove:
            raise _Boom("remove")


def _recording_forge(history: list[tuple[Any, ...]]) -> Forge:
    return cast("Forge", _RecordingForge(history))


def _tracked(
    history: list[tuple[Any, ...]], labels: Sequence[str], fail_after: int | None = None
) -> Iterator[str]:
    """A lazy label source whose evaluation order is visible in the history."""
    for index, label in enumerate(labels):
        if fail_after is not None and index == fail_after:
            history.append(("iterate-error", index))
            raise _Boom("iterate")
        history.append(("yield", label))
        yield label


@dataclass(frozen=True)
class _Scenario:
    new_label: str
    old_labels: tuple[str, ...]
    fail_add: bool = False
    fail_remove: str | None = None
    callback: str | None = None  # None | "ok" | "raises"
    iterate_fail_after: int | None = None


_HISTORY_SCENARIOS = {
    "success": _Scenario(
        "status:in-progress", ("status:queued", "status:blocked"), callback="ok"
    ),
    "success-without-callback": _Scenario(
        "status:done", ("status:in-progress", "status:queued")
    ),
    "empty-old-labels": _Scenario("status:in-progress", (), callback="ok"),
    "add-fails": _Scenario(
        "status:done", ("status:in-progress",), fail_add=True, callback="ok"
    ),
    "callback-raises": _Scenario(
        "status:done", ("status:in-progress", "status:queued"), callback="raises"
    ),
    "first-remove-fails": _Scenario(
        "status:done",
        ("status:in-progress", "status:queued"),
        fail_remove="status:in-progress",
        callback="ok",
    ),
    "middle-remove-fails": _Scenario(
        "status:done",
        ("status:in-progress", "status:queued", "status:blocked"),
        fail_remove="status:queued",
        callback="ok",
    ),
    "iterable-fails-midway": _Scenario(
        "status:done",
        ("status:in-progress", "status:queued", "status:blocked"),
        iterate_fail_after=1,
        callback="ok",
    ),
    "iterable-fails-before-first": _Scenario(
        "status:done", ("status:in-progress",), iterate_fail_after=0, callback="ok"
    ),
    "duplicates-are-removed-each-time": _Scenario(
        "status:done",
        ("status:queued", "status:queued", "status:blocked", "status:queued"),
        callback="ok",
    ),
    "self-label-is-skipped-but-evaluated": _Scenario(
        "status:blocked",
        ("status:queued", "status:blocked", "status:queued"),
        callback="ok",
    ),
    "only-self-label": _Scenario("status:queued", ("status:queued",), callback="ok"),
    "unknown-labels": _Scenario(
        "custom:new", ("custom:old", "", "status:unknown", "status:done"), callback="ok"
    ),
    "escalation-and-final-labels-are-not-pruned": _Scenario(
        "status:queued",
        ("status:done", "status:blocked-human-review"),
        callback="ok",
    ),
}


def _run(
    implementation: Callable[..., None],
    scenario: _Scenario,
    old_labels_kind: str,
) -> tuple[list[tuple[Any, ...]], str | None]:
    history: list[tuple[Any, ...]] = []
    forge = _RecordingForge(history, scenario.fail_add, scenario.fail_remove)
    old_labels: Iterable[str]
    if old_labels_kind == "generator":
        old_labels = _tracked(history, scenario.old_labels, scenario.iterate_fail_after)
    else:
        old_labels = scenario.old_labels
    callback: Callable[[], None] | None = None
    if scenario.callback == "ok":
        callback = lambda: history.append(("callback",))  # noqa: E731
    elif scenario.callback == "raises":

        def callback() -> None:
            history.append(("callback",))
            raise _Boom("callback")

    error: str | None = None
    try:
        implementation(forge, 7, scenario.new_label, old_labels, callback)
    except _Boom as exc:
        error = f"_Boom({exc})"
    return history, error


class TestOperationHistoryMatchesLegacyAdapter:
    @pytest.mark.parametrize("scenario_name", sorted(_HISTORY_SCENARIOS))
    @pytest.mark.parametrize("old_labels_kind", ["tuple", "generator"])
    def test_history_and_exception_are_identical(
        self, scenario_name: str, old_labels_kind: str
    ) -> None:
        scenario = _HISTORY_SCENARIOS[scenario_name]
        if old_labels_kind == "tuple" and scenario.iterate_fail_after is not None:
            pytest.skip("iterator failures need a lazy Iterable")

        expected = _run(_legacy_transition_status_label, scenario, old_labels_kind)
        actual = _run(transition_status_label, scenario, old_labels_kind)

        assert actual == expected

    def test_generator_is_evaluated_between_removes_not_before_add(self) -> None:
        history: list[tuple[Any, ...]] = []
        forge = _recording_forge(history)

        transition_status_label(
            forge,
            7,
            "status:done",
            _tracked(history, ("status:queued", "status:blocked")),
            lambda: history.append(("callback",)),
        )

        assert history == [
            ("add", 7, "status:done"),
            ("callback",),
            ("yield", "status:queued"),
            ("remove", 7, "status:queued"),
            ("yield", "status:blocked"),
            ("remove", 7, "status:blocked"),
        ]

    def test_new_label_that_already_exists_is_still_added(self) -> None:
        history: list[tuple[Any, ...]] = []
        transition_status_label(
            _recording_forge(history), 7, "status:blocked", ("status:blocked",)
        )
        assert history == [("add", 7, "status:blocked")]

    def test_callback_runs_on_every_successful_add_including_retries(self) -> None:
        calls: list[str] = []
        forge = _recording_forge([])
        for _ in range(2):
            transition_status_label(
                forge, 7, "status:done", ("status:queued",), lambda: calls.append("cb")
            )
        assert calls == ["cb", "cb"]

    def test_incomplete_old_labels_leave_the_unlisted_label_in_place(self) -> None:
        # queued + done held, only queued listed: done survives (current behaviour).
        labels = {"status:queued", "status:done"}

        class _StatefulForge:
            def add_label(self, _issue: Any, label: str) -> None:
                labels.add(label)

            def remove_label(self, _issue: Any, label: str) -> None:
                labels.discard(label)

        transition_status_label(
            cast("Forge", _StatefulForge()), 7, "status:in-progress", ("status:queued",)
        )

        assert labels == {"status:done", "status:in-progress"}


class TestStatusLabelConstantsAreDerivedFromTheRoleTable:
    def test_primary_status_labels_keep_type_value_and_order(self) -> None:
        assert type(PRIMARY_STATUS_LABELS) is tuple
        assert PRIMARY_STATUS_LABELS == (
            StatusLabel.IN_PROGRESS,
            StatusLabel.QUEUED,
            StatusLabel.BLOCKED,
        )
        assert all(type(label) is StatusLabel for label in PRIMARY_STATUS_LABELS)

    def test_terminal_escalation_labels_keep_type_value_and_order(self) -> None:
        assert type(TERMINAL_ESCALATION_LABELS) is tuple
        assert TERMINAL_ESCALATION_LABELS == (
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        )
        assert all(type(label) is StatusLabel for label in TERMINAL_ESCALATION_LABELS)

    def test_constants_agree_with_the_declared_roles(self) -> None:
        assert set(PRIMARY_STATUS_LABELS) == {
            label for label, role in LABEL_ROLES.items() if role is LabelRole.ACTIVE
        }
        assert set(TERMINAL_ESCALATION_LABELS) == {
            label for label, role in LABEL_ROLES.items() if role is LabelRole.ESCALATION
        }

    def test_consistency_kernel_keeps_its_own_seven_lifecycle_contract(self) -> None:
        from orchestune.consistency.invariants import status as consistency_status

        assert len(consistency_status.PRIMARY_STATUS_LABELS) == 7
        assert set(consistency_status.PRIMARY_STATUS_LABELS) == set(
            lifecycle_labels(tuple(StatusLabel))
        )
        assert consistency_status.PRIMARY_STATUS_LABELS != PRIMARY_STATUS_LABELS
        assert set(PRIMARY_STATUS_LABELS) < set(
            consistency_status.PRIMARY_STATUS_LABELS
        )
        assert (
            consistency_status.TERMINAL_ESCALATION_LABELS == TERMINAL_ESCALATION_LABELS
        )
