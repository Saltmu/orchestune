"""Generation and fault boundaries for durable not-needed review holds."""

from dataclasses import replace

import pytest

from orchestune.complete.service import complete_task
from orchestune.outcome_record import OutcomeRecord
from tests.status_event_review_support import ISSUE, LabeledReviewWorld

PENDING = "not-needed-review:pending"


@pytest.mark.parametrize("verdict", ["passed", "failed", "timeout"])
def test_review_resolution_removes_pending_only_after_safe_transition(
    tmp_path, monkeypatch, verdict
):
    world = LabeledReviewWorld(tmp_path, monkeypatch)
    world.complete()
    world.run_policies(100)
    assert PENDING in world.forge.labels
    if verdict != "timeout":
        world.post_verdict(verdict)
    world.run_policies(111 if verdict == "timeout" else 101)
    assert PENDING not in world.forge.labels
    world.lose_ledger()
    assert world.observe().done == (verdict == "passed")
    if verdict == "passed":
        assert world.forge.operations.index("close") < world.forge.operations.index(
            "remove:" + PENDING
        )
    else:
        assert world.forge.operations.index(
            "remove:status:not-needed"
        ) < world.forge.operations.index("remove:" + PENDING)


@pytest.mark.parametrize(
    "failure",
    [
        "close",
        "comments",
        "new-generation",
        "corrupt-latest",
        "conflicting-verdict",
        "remove",
        "confirm-remove",
    ],
)
def test_ambiguous_or_failed_release_keeps_dependency_held(
    tmp_path, monkeypatch, failure
):
    world = LabeledReviewWorld(tmp_path, monkeypatch)
    world.complete()
    world.run_policies(100)
    world.post_verdict("passed")
    if failure == "close":
        monkeypatch.setattr(
            world.forge,
            "close_issue",
            lambda *a: (_ for _ in ()).throw(OSError("close failed")),
        )
    elif failure == "comments":
        monkeypatch.setattr(
            world.forge,
            "list_all_issue_comments",
            lambda *a: (_ for _ in ()).throw(OSError("pagination failed")),
        )
    elif failure == "new-generation":
        world.forge.add_comment(
            ISSUE,
            OutcomeRecord("not-needed", ISSUE, completion_id="new-generation").render(),
        )
    elif failure == "corrupt-latest":
        world.forge.add_comment(ISSUE, "<!-- orchestune:outcome -->\n```json\n{}\n```")
    elif failure == "conflicting-verdict":
        world.post_verdict("failed")
        # Failing queued transition preserves pending even though pass exists.
        monkeypatch.setattr(
            world.forge,
            "add_label",
            lambda *a: (_ for _ in ()).throw(OSError("transition failed")),
        )
    elif failure == "remove":
        original = world.forge.remove_label
        monkeypatch.setattr(
            world.forge,
            "remove_label",
            lambda issue, label: (_ for _ in ()).throw(OSError("remove failed"))
            if label == PENDING
            else original(issue, label),
        )
    else:
        monkeypatch.setattr(world.forge, "remove_label", lambda *a: None)
    world.run_policies(101)
    assert PENDING in world.forge.labels
    world.lose_ledger()
    assert not world.observe().done


def test_legacy_journal_backfills_hold_without_editing_fixed_outcome(
    tmp_path, monkeypatch
):
    from orchestune.dispatch.gc.policy_discovery import confirmed_records, update_record
    from orchestune.infra.process_utils import run_state_lock
    from orchestune.ledger.run_state import load_run_state_readonly, save_run_state
    from orchestune.outcome_record import parse_from_comments

    world = LabeledReviewWorld(tmp_path, monkeypatch)
    world.complete()
    state = load_run_state_readonly(world.path)
    record = confirmed_records(state)[0]
    outcome = parse_from_comments(world.forge.comments)
    body = replace(outcome, not_needed_review=None).render()
    world.forge.comments[0]["body"] = body
    world.forge.labels.remove(PENDING)
    record = replace(record, outcome_payload={**record.outcome_payload, "body": body})
    update_record(state, record)
    state.completion_reservations[record.reservation_key]["outcome_payload"] = (
        record.outcome_payload
    )
    with run_state_lock(world.path.with_suffix(".lock")):
        save_run_state(state, world.path)
    world.run_policies(100)
    assert PENDING in world.forge.labels
    assert world.forge.comments[0]["body"] == body
    world.lose_ledger()
    assert not world.observe().done
    world.run_policies(101)
    world.config.dispatch_target.fire_text.assert_called_once()


def test_publication_response_loss_reuses_fixed_body_and_hold(tmp_path, monkeypatch):
    from tests.complete_lifecycle_test_support import unclaimed_environment

    request, forge = unclaimed_environment(tmp_path, monkeypatch)
    fired = False

    def inject(operation, after):
        nonlocal fired
        if operation == "add" and after and not fired:
            fired = True
            raise OSError("response lost")

    forge.inject = inject
    failed = complete_task(request, forge=forge)
    assert not failed.success and PENDING in forge.labels and not forge.comments
    forge.inject = lambda *_: None
    resumed = complete_task(
        replace(request, completion_id=failed.completion_id), forge=forge
    )
    assert resumed.success and len(forge.comments) == 1
    assert forge.operations.count("add:" + PENDING) == 1
