"""#827: durable, bounded accounting of remote-denied child-branch deletions."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from orchestune.forge import REQUIRED_LABELS, Forge
from orchestune.integrator.finalization_retry import (
    BLOCKED_LABEL,
    CHILD_BRANCH_DELETION_DENIAL_LIMIT,
    BlockedOutcome,
    DenialVerdict,
    escalate_terminal,
    handle_denied_deletion,
    latest_blocked_proof,
    record_denial,
    settle_blocked_child,
)
from orchestune.integrator.proofs import TaskIntegrationProof

CHILD = 1
PARENT = 100
BASE = "origin/parent/issue-100"
SHA = "a" * 40


def make_proof(sha: str = SHA, branch: str = "claude/issue-1-task-1"):
    return TaskIntegrationProof(
        issue_number=CHILD, subtask_id="task-1", branch_name=branch, source_sha=sha
    )


class MemoryForge:
    """Just the Forge surface the accounting uses, backed by memory."""

    def __init__(self) -> None:
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.labels: dict[int, set[str]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.failing: set[str] = set()
        self.failing_comment_issues: set[int] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.failing:
            raise RuntimeError(f"{name} failed")

    def get_authenticated_user(self) -> str:
        self._maybe_fail("get_authenticated_user")
        return "bot"

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self._maybe_fail("list_comments")
        return [dict(c) for c in self.comments.get(issue_number, [])]

    def add_comment(self, issue_number: int, body: str) -> None:
        self._maybe_fail("add_comment")
        if issue_number in self.failing_comment_issues:
            raise RuntimeError(f"comment on #{issue_number} failed")
        self.calls.append(("comment", issue_number))
        self.comments.setdefault(issue_number, []).append(
            {"body": body, "author": "bot", "created_at": ""}
        )

    def add_label(self, issue_number: int, label: str) -> None:
        self._maybe_fail("add_label")
        self.calls.append(("add_label", issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self._maybe_fail("remove_label")
        self.calls.append(("remove_label", issue_number, label))
        self.labels.setdefault(issue_number, set()).discard(label)

    def ensure_labels(self, specs: tuple[Any, ...]) -> None:
        self._maybe_fail("ensure_labels")
        self.calls.append(("ensure_labels", tuple(spec.name for spec in specs)))

    def comment_count(self, issue_number: int) -> int:
        return len(self.comments.get(issue_number, []))


def record(forge: MemoryForge, run_id: str, proof=None) -> DenialVerdict:
    return record_denial(cast(Forge, forge), proof or make_proof(), BASE, run_id)


def test_the_limit_is_three_denials():
    assert CHILD_BRANCH_DELETION_DENIAL_LIMIT == 3


def test_blocked_label_is_a_required_repository_label():
    assert BLOCKED_LABEL == "integration:finalization-blocked"
    assert BLOCKED_LABEL in {spec.name for spec in REQUIRED_LABELS}


def test_each_distinct_run_counts_once_until_the_limit():
    forge = MemoryForge()

    assert record(forge, "run-1") is DenialVerdict.RECORDED
    assert record(forge, "run-2") is DenialVerdict.RECORDED
    assert record(forge, "run-3") is DenialVerdict.LIMIT_REACHED
    assert forge.comment_count(CHILD) == 3


def test_the_same_run_is_not_counted_twice():
    forge = MemoryForge()

    record(forge, "run-1")
    record(forge, "run-1")

    assert forge.comment_count(CHILD) == 1


def test_the_count_is_rebuilt_from_issue_comments_not_process_state():
    first_runner = MemoryForge()
    record(first_runner, "run-1")
    record(first_runner, "run-2")

    second_runner = MemoryForge()
    second_runner.comments = first_runner.comments

    assert record(second_runner, "run-3") is DenialVerdict.LIMIT_REACHED


def test_a_moved_tip_or_other_branch_starts_a_new_count():
    forge = MemoryForge()
    record(forge, "run-1")
    record(forge, "run-2")

    assert record(forge, "run-3", make_proof(sha="b" * 40)) is DenialVerdict.RECORDED
    assert (
        record(forge, "run-3", make_proof(branch="claude/issue-1-other"))
        is DenialVerdict.RECORDED
    )


def test_another_base_branch_starts_a_new_count():
    forge = MemoryForge()
    record(forge, "run-1")
    record(forge, "run-2")

    verdict = record_denial(forge, make_proof(), "origin/parent/issue-200", "run-3")

    assert verdict is DenialVerdict.RECORDED


def test_comments_from_other_authors_are_not_counted():
    forge = MemoryForge()
    record(forge, "run-1")
    record(forge, "run-2")
    for comment in list(forge.comments[CHILD]):
        forge.comments[CHILD].append({**comment, "author": "someone-else"})

    assert record(forge, "run-3") is DenialVerdict.LIMIT_REACHED
    # The forged copies did not push the count past the limit earlier either.
    assert forge.comment_count(CHILD) == 5


def test_a_non_canonical_comment_is_not_counted():
    forge = MemoryForge()
    record(forge, "run-1")
    forged = dict(forge.comments[CHILD][0])
    forged["body"] += "\nextra"
    forge.comments[CHILD].append(forged)
    forge.comments[CHILD].append(
        {
            "body": "<!-- orchestune:child-branch-finalization:v1 -->\nnot json",
            "author": "bot",
            "created_at": "",
        }
    )

    assert record(forge, "run-2") is DenialVerdict.RECORDED


def test_an_unreadable_history_never_advances_or_escalates():
    forge = MemoryForge()
    forge.failing = {"list_comments"}

    assert record(forge, "run-1") is DenialVerdict.UNKNOWN
    assert forge.calls == []


def test_an_unverifiable_author_never_advances_the_count():
    forge = MemoryForge()
    forge.failing = {"get_authenticated_user"}

    assert record(forge, "run-1") is DenialVerdict.UNKNOWN
    assert forge.calls == []


def test_a_failed_write_does_not_count_the_attempt():
    forge = MemoryForge()
    record(forge, "run-1")
    record(forge, "run-2")
    forge.failing = {"add_comment"}

    assert record(forge, "run-3") is DenialVerdict.UNKNOWN

    forge.failing = set()
    assert record(forge, "run-3") is DenialVerdict.LIMIT_REACHED


def test_reaching_the_limit_stays_pending_until_a_terminal_record_exists():
    forge = MemoryForge()
    for run in ("run-1", "run-2", "run-3"):
        record(forge, run)

    # Same run again: nothing new is written, the pending escalation is reported.
    assert record(forge, "run-3") is DenialVerdict.LIMIT_REACHED
    assert forge.comment_count(CHILD) == 3
    # A later run (escalation still failing) also reports it without a 4th count.
    assert record(forge, "run-4") is DenialVerdict.LIMIT_REACHED


def test_escalation_labels_comments_the_parent_then_records_the_terminal_event():
    forge = MemoryForge()
    forge.labels[CHILD] = {"status:done"}
    for run in ("run-1", "run-2", "run-3"):
        record(forge, run)
    forge.calls.clear()

    assert escalate_terminal(forge, make_proof(), BASE, PARENT) is True

    assert forge.calls == [
        ("ensure_labels", (BLOCKED_LABEL,)),
        ("add_label", CHILD, BLOCKED_LABEL),
        ("comment", PARENT),
        ("comment", CHILD),
    ]
    assert forge.labels[CHILD] == {"status:done", BLOCKED_LABEL}
    parent_body = forge.comments[PARENT][0]["body"]
    assert f"issue={CHILD} sha={SHA}" in parent_body
    assert "#1" in parent_body


def test_escalation_never_touches_status_labels():
    forge = MemoryForge()
    forge.labels[CHILD] = {"status:done"}

    escalate_terminal(forge, make_proof(), BASE, PARENT)

    assert not [c for c in forge.calls if c[0] == "remove_label"]
    assert [c for c in forge.calls if c[0] == "add_label"] == [
        ("add_label", CHILD, BLOCKED_LABEL)
    ]


def test_escalation_is_idempotent():
    forge = MemoryForge()
    escalate_terminal(forge, make_proof(), BASE, PARENT)
    before = (forge.comment_count(PARENT), forge.comment_count(CHILD))

    assert escalate_terminal(forge, make_proof(), BASE, PARENT) is True

    assert (forge.comment_count(PARENT), forge.comment_count(CHILD)) == before


@pytest.mark.parametrize(
    ("failing", "failing_issue", "expected_comments"),
    [
        ({"add_label"}, None, {PARENT: 0, CHILD: 0}),
        (set(), PARENT, {PARENT: 0, CHILD: 0}),
        (set(), CHILD, {PARENT: 1, CHILD: 0}),
    ],
)
def test_a_failed_escalation_step_leaves_no_terminal_record(
    failing, failing_issue, expected_comments
):
    forge = MemoryForge()
    forge.failing = failing
    if failing_issue is not None:
        forge.failing_comment_issues = {failing_issue}

    assert escalate_terminal(forge, make_proof(), BASE, PARENT) is False

    assert {
        PARENT: forge.comment_count(PARENT),
        CHILD: forge.comment_count(CHILD),
    } == expected_comments


def test_a_retried_escalation_does_not_repeat_the_parent_comment():
    forge = MemoryForge()
    forge.failing_comment_issues = {CHILD}
    escalate_terminal(forge, make_proof(), BASE, PARENT)

    forge.failing_comment_issues = set()
    assert escalate_terminal(forge, make_proof(), BASE, PARENT) is True

    assert forge.comment_count(PARENT) == 1
    assert forge.comment_count(CHILD) == 1


def test_the_count_restarts_after_a_terminal_record():
    forge = MemoryForge()
    for run in ("run-1", "run-2", "run-3"):
        record(forge, run)
    escalate_terminal(forge, make_proof(), BASE, PARENT)

    assert record(forge, "run-4") is DenialVerdict.RECORDED
    assert record(forge, "run-5") is DenialVerdict.RECORDED
    assert record(forge, "run-6") is DenialVerdict.LIMIT_REACHED


def test_handle_denied_deletion_escalates_only_at_the_limit():
    forge = MemoryForge()

    first = handle_denied_deletion(forge, make_proof(), BASE, "run-1", PARENT)
    second = handle_denied_deletion(forge, make_proof(), BASE, "run-2", PARENT)

    assert (first, second) == (DenialVerdict.RECORDED, DenialVerdict.RECORDED)
    assert BLOCKED_LABEL not in forge.labels.get(CHILD, set())

    third = handle_denied_deletion(forge, make_proof(), BASE, "run-3", PARENT)

    assert third is DenialVerdict.ESCALATED
    assert BLOCKED_LABEL in forge.labels[CHILD]


def test_handle_denied_deletion_reports_a_pending_escalation_and_retries():
    forge = MemoryForge()
    handle_denied_deletion(forge, make_proof(), BASE, "run-1", PARENT)
    handle_denied_deletion(forge, make_proof(), BASE, "run-2", PARENT)
    forge.failing = {"add_label"}

    assert (
        handle_denied_deletion(forge, make_proof(), BASE, "run-3", PARENT)
        is DenialVerdict.LIMIT_REACHED
    )

    forge.failing = set()
    assert (
        handle_denied_deletion(forge, make_proof(), BASE, "run-4", PARENT)
        is DenialVerdict.ESCALATED
    )


def _escalated_forge() -> MemoryForge:
    forge = MemoryForge()
    for run in ("run-1", "run-2", "run-3"):
        handle_denied_deletion(cast(Forge, forge), make_proof(), BASE, run, PARENT)
    forge.calls.clear()
    return forge


def test_a_blocked_child_whose_branch_is_still_there_holds_without_writes():
    forge = _escalated_forge()

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: SHA)

    assert outcome is BlockedOutcome.HOLD
    assert forge.calls == []


def test_a_blocked_child_whose_branch_was_removed_is_finalized():
    forge = _escalated_forge()

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: None)

    assert outcome is BlockedOutcome.FINALIZE
    assert forge.calls == [("remove_label", CHILD, BLOCKED_LABEL)]


def test_a_blocked_child_whose_branch_moved_goes_back_to_integration():
    forge = _escalated_forge()

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: "b" * 40)

    assert outcome is BlockedOutcome.REINTEGRATE
    assert forge.calls == [("remove_label", CHILD, BLOCKED_LABEL)]


def test_a_failed_label_removal_keeps_a_moved_tip_from_looping_back():
    forge = _escalated_forge()
    forge.failing = {"remove_label"}

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: "b" * 40)

    assert outcome is BlockedOutcome.HOLD


def test_a_failed_label_removal_does_not_stop_a_removed_branch_finalizing():
    forge = _escalated_forge()
    forge.failing = {"remove_label"}

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: None)

    assert outcome is BlockedOutcome.FINALIZE


def test_an_unreadable_remote_never_finalizes_or_reintegrates():
    forge = _escalated_forge()

    def unreadable() -> str | None:
        raise OSError("ls-remote failed")

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, unreadable)

    assert outcome is BlockedOutcome.HOLD
    assert forge.calls == []


def test_a_blocked_child_missing_its_terminal_record_gets_it_posted():
    forge = MemoryForge()
    forge.labels[CHILD] = {BLOCKED_LABEL}
    for run in ("run-1", "run-2", "run-3"):
        record(forge, run)

    outcome = settle_blocked_child(forge, make_proof(), BASE, PARENT, lambda: SHA)

    assert outcome is BlockedOutcome.HOLD
    assert forge.comment_count(PARENT) == 1
    terminal = forge.comments[CHILD][-1]["body"]
    assert (
        json.loads(terminal.split("```json\n", 1)[1].split("\n```", 1)[0])["event"]
        == "terminal"
    )


def test_escalation_with_an_unreadable_history_writes_nothing():
    forge = MemoryForge()
    forge.failing = {"list_comments"}

    assert escalate_terminal(cast(Forge, forge), make_proof(), BASE, PARENT) is False

    assert forge.calls == []


def test_a_failed_label_self_repair_does_not_stop_the_escalation():
    """The label may already exist; `add_label` is the step that must succeed."""
    forge = MemoryForge()
    forge.failing = {"ensure_labels"}

    assert escalate_terminal(cast(Forge, forge), make_proof(), BASE, PARENT) is True

    assert forge.labels[CHILD] == {BLOCKED_LABEL}


def test_ordinary_comments_on_the_issue_are_ignored():
    forge = MemoryForge()
    forge.comments[CHILD] = [
        {"body": "LGTM, thanks!", "author": "bot", "created_at": ""},
        {"body": None, "author": "bot", "created_at": ""},
    ]

    assert record(forge, "run-1") is DenialVerdict.RECORDED


def test_the_blocked_proof_comes_from_the_newest_event_not_the_oldest():
    """A tip that moved and was re-integrated must not resurrect the old one."""
    forge = MemoryForge()
    old, new = make_proof(sha="a" * 40), make_proof(sha="b" * 40)
    record(forge, "run-1", old)
    for run in ("run-2", "run-3", "run-4"):
        record(forge, run, new)

    proof = latest_blocked_proof(cast(Forge, forge), CHILD, "task-1", BASE)

    assert proof == new


def test_the_blocked_proof_is_none_without_events_or_when_unreadable():
    forge = MemoryForge()
    assert latest_blocked_proof(cast(Forge, forge), CHILD, "task-1", BASE) is None

    record(forge, "run-1")
    forge.failing = {"list_comments"}
    assert latest_blocked_proof(cast(Forge, forge), CHILD, "task-1", BASE) is None


def test_the_blocked_proof_ignores_other_children_bases_and_authors():
    forge = MemoryForge()
    record(forge, "run-1")
    forge.comments[CHILD].append({**forge.comments[CHILD][0], "author": "other"})

    assert latest_blocked_proof(cast(Forge, forge), CHILD, "other-task", BASE) is None
    assert (
        latest_blocked_proof(
            cast(Forge, forge), CHILD, "task-1", "origin/parent/issue-9"
        )
        is None
    )
    assert latest_blocked_proof(cast(Forge, forge), CHILD, "task-1", BASE) == (
        make_proof()
    )


def test_the_blocked_proof_rejects_an_event_naming_an_invalid_sha():
    from orchestune.integrator import finalization_retry as module

    forge = MemoryForge()
    payload = {
        "event": "terminal",
        "attempts": 3,
        "issue_number": CHILD,
        "subtask_id": "task-1",
        "branch_name": "claude/issue-1-task-1",
        "source_sha": "not-a-sha",
        "base_branch": "parent/issue-100",
    }
    forge.comments[CHILD] = [
        {"body": module._render(payload), "author": "bot", "created_at": ""}
    ]

    assert latest_blocked_proof(cast(Forge, forge), CHILD, "task-1", BASE) is None
