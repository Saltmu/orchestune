"""Failure-ordering contract for the shared ledger escalation operation."""

from __future__ import annotations

from importlib import import_module

import pytest


@pytest.fixture
def escalation():
    return import_module("orchestune.ledger.escalation")


def test_escalation_order_is_add_callback_remove_comment(
    fake_forge, escalation
) -> None:
    events: list[tuple[str, object]] = []
    fake_forge.add_label.side_effect = lambda issue, label: events.append(
        ("add", label)
    )
    fake_forge.remove_label.side_effect = lambda issue, label: events.append(
        ("remove", label)
    )
    fake_forge.add_comment.side_effect = lambda issue, comment: events.append(
        ("comment", comment)
    )

    escalation.apply_human_review_escalation(
        1062,
        ("status:in-progress",),
        "needs human review",
        forge=fake_forge,
        on_label_applied=lambda: events.append(("callback", "saved")),
    )

    assert events == [
        ("add", "status:blocked-human-review"),
        ("callback", "saved"),
        ("remove", "status:in-progress"),
        ("comment", "needs human review"),
    ]


@pytest.mark.parametrize("failure", ["add", "callback"])
def test_early_failure_stops_later_escalation_steps(
    fake_forge, escalation, failure: str
) -> None:
    callback_calls: list[bool] = []

    if failure == "add":
        fake_forge.add_label.side_effect = RuntimeError("add failed")

    def callback() -> None:
        callback_calls.append(True)
        if failure == "callback":
            raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        escalation.apply_human_review_escalation(
            1062,
            ("status:in-progress",),
            "needs human review",
            forge=fake_forge,
            on_label_applied=callback,
        )

    assert callback_calls == ([True] if failure == "callback" else [])
    if failure == "add":
        fake_forge.add_label.assert_called_once_with(
            1062, "status:blocked-human-review"
        )
    else:
        fake_forge.add_label.assert_called_once_with(
            1062, "status:blocked-human-review"
        )
    fake_forge.remove_label.assert_not_called()
    fake_forge.add_comment.assert_not_called()


@pytest.mark.parametrize("failure", ["remove", "comment"])
def test_later_failure_keeps_the_callback_commit(
    fake_forge, escalation, failure: str
) -> None:
    committed: list[str] = []
    if failure == "remove":
        fake_forge.remove_label.side_effect = RuntimeError("remove failed")
    else:
        fake_forge.add_comment.side_effect = RuntimeError("comment failed")

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        escalation.apply_human_review_escalation(
            1062,
            ("status:in-progress",),
            "needs human review",
            forge=fake_forge,
            on_label_applied=lambda: committed.append("saved"),
        )

    assert committed == ["saved"]
    fake_forge.add_label.assert_called_once_with(1062, "status:blocked-human-review")
    fake_forge.remove_label.assert_called_once_with(1062, "status:in-progress")


def test_removable_labels_include_not_needed(escalation) -> None:
    assert escalation._REMOVABLE_STATUS_LABELS == (
        "status:in-progress",
        "status:queued",
        "status:blocked",
        "status:not-needed",
    )
