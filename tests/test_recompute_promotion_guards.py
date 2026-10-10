"""Recompute release uses fresh apply guards and a side-effect-free snapshot preview."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestune.dispatch import reconciliation
from orchestune.dispatch.cycle_context import _build_task_mappings
from orchestune.ledger.run_state import RunState
from tests.conftest import make_issue
from tests.dependency_liveness_test_support import (
    BASE_RED,
    DEPENDENT,
    RECOMPUTE,
    CompletionPath,
    LivenessWorld,
)
from tests.dispatch_test_support import make_test_cycle_context


def test_recompute_release_observes_late_hold(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    observation = world.cycle(before_promotion=lambda w: w.set_t_label(BASE_RED, True))
    assert not observation.promoted
    assert "status:blocked" in world.labels() and BASE_RED in world.labels()
    assert RECOMPUTE not in world.labels()


@pytest.mark.parametrize("apply", [False, True])
def test_recompute_hold_does_not_report_a_promotion(tmp_path, apply):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE, BASE_RED))
    world.complete(11, CompletionPath.LABEL)
    before = dict(world.forge.issues)
    state_before = world.run_state_path.read_bytes()
    observation = world.cycle(apply=apply)
    assert not observation.promoted and not observation.previewed
    assert "status:blocked" in world.labels() and BASE_RED in world.labels()
    if not apply:
        assert world.forge.issues == before
        assert world.run_state_path.read_bytes() == state_before
        assert not world.boundary.history


@pytest.mark.parametrize(
    "labels",
    [
        ("status:done", RECOMPUTE),
        ("status:not-needed", RECOMPUTE),
        ("status:blocked", "status:blocked-human-review", RECOMPUTE),
        ("status:blocked", "status:manual-merge-required", RECOMPUTE),
        ("status:in-progress", RECOMPUTE),
        ("status:queued", RECOMPUTE),
    ],
)
def test_recompute_release_observes_late_protected_status(tmp_path, labels):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    observation = world.cycle(before_promotion=lambda w: w.relabel(DEPENDENT, labels))
    assert not observation.promotion_issue_numbers
    assert world.labels() == set(labels)
    assert RECOMPUTE in world.labels()


def test_recompute_release_observes_late_close(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)

    def close(w):
        w.forge.issues[DEPENDENT] = replace(w.forge.issues[DEPENDENT], state="CLOSED")

    assert not world.cycle(before_promotion=close).promoted
    assert RECOMPUTE in world.labels()


@pytest.mark.parametrize("number", [11, DEPENDENT])
@pytest.mark.parametrize("apply", [False, True])
def test_recompute_respects_completion_reservations(tmp_path, number, apply):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    world.set_reservation(number, True)
    observation = world.cycle(apply=apply)
    assert not observation.promoted and not observation.previewed


@pytest.mark.parametrize(
    "path",
    [
        CompletionPath.LABEL,
        CompletionPath.RECORD_COMPLETION,
        CompletionPath.PRIOR_MERGE,
    ],
)
def test_safe_recompute_release_promotes_in_same_cycle_once(tmp_path, path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, path)
    first = world.cycle()
    assert first.promoted and first.promotion_issue_numbers == (DEPENDENT,)
    assert world.labels() == {"status:queued"}
    assert not world.cycle().promotion_issue_numbers


def _recovery_snapshot(tmp_path, *, apply=True):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    records = dict(world.forge.issues)
    tasks, _, _ = _build_task_mappings(list(records.values()))
    state = RunState()
    config = world.config(apply=apply)
    ctx = make_test_cycle_context(
        tasks_by_issue=tasks,
        resolve_dependencies=True,
        run_state=state,
        config=config,
        issue_records_by_number=records,
    )

    def recover(conflicts=None):
        return reconciliation._resolve_one_blocked_recompute_issue(
            records[DEPENDENT],
            ctx.task(DEPENDENT),
            conflicts or set(),
            ctx,
            state,
            config,
        )

    return world, ctx, recover


@pytest.mark.parametrize("phase", ["initial", "after-release", "verification"])
def test_recompute_read_failure_reports_no_success(tmp_path, phase):
    world, ctx, recover = _recovery_snapshot(tmp_path)
    original = world.boundary.get_issue
    calls = 0

    def get_issue(number):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "initial" else 2):
            raise OSError("fresh read unavailable")
        return original(number)

    target = "get_issue_state" if phase == "verification" else "get_issue"
    effect = (
        OSError("verification unavailable") if phase == "verification" else get_issue
    )
    with patch.object(world.boundary, target, side_effect=effect):
        assert recover() is None
    assert ctx.task(DEPENDENT).status_labels == ("status:blocked", RECOMPUTE)
    if phase != "verification":
        assert "status:queued" not in world.labels()


@pytest.mark.parametrize("lost_response", [False, True])
def test_marker_removal_failure_does_not_promote(tmp_path, lost_response):
    world, ctx, recover = _recovery_snapshot(tmp_path)
    original = world.boundary.remove_label

    def remove(number, label):
        if lost_response:
            original(number, label)
        raise OSError("marker removal failed")

    with patch.object(world.boundary, "remove_label", side_effect=remove):
        assert recover() is None
    assert "status:queued" not in world.labels()
    assert ctx.task(DEPENDENT).status_labels == ("status:blocked", RECOMPUTE)


@pytest.mark.parametrize(
    "change", ["recompute", "hold", "closed", "human", "declaration"]
)
def test_revalidate_subject_after_marker_release(tmp_path, change):
    world, ctx, recover = _recovery_snapshot(tmp_path)
    original = world.boundary.remove_label

    def remove(number, label):
        original(number, label)
        if change == "closed":
            world.forge.issues[DEPENDENT] = replace(
                world.forge.issues[DEPENDENT], state="CLOSED"
            )
        elif change == "declaration":
            world.forge.issues[DEPENDENT] = make_issue(
                DEPENDENT,
                labels=("status:blocked",),
                subtask_id="dependent",
                depends_on=("dep-a", "missing-new-dependency"),
            )
        else:
            added = {
                "recompute": RECOMPUTE,
                "hold": BASE_RED,
                "human": "status:blocked-human-review",
            }[change]
            world.set_t_label(added, True)

    with patch.object(world.boundary, "remove_label", side_effect=remove):
        assert recover() is None
    assert "status:queued" not in world.labels()
    assert ctx.task(DEPENDENT).status_labels == ("status:blocked", RECOMPUTE)


@pytest.mark.parametrize(
    "failure", ["missing", "unknown", "waiting", "unresolved", "ci-only"]
)
def test_recompute_dependency_failure_is_closed(tmp_path, failure):
    world, _, recover = _recovery_snapshot(tmp_path)
    if failure in ("waiting", "ci-only"):
        world.revoke(11)
    if failure == "ci-only":
        # Passing CI remains nonterminal evidence for this dependency.
        world.forge.issues[11] = replace(
            world.forge.issues[11], labels=("status:in-progress",)
        )
    if failure == "unresolved":
        world.forge.issues[DEPENDENT] = make_issue(
            DEPENDENT, labels=("status:blocked", RECOMPUTE), depends_on=("missing",)
        )
    if failure in ("missing", "unknown"):
        evaluation = (
            None
            if failure == "missing"
            else SimpleNamespace(
                task=_build_task_mappings(
                    [replace(world.forge.issues[DEPENDENT], labels=("status:blocked",))]
                )[0][DEPENDENT],
                assessment=None,
            )
        )
        with patch.object(
            reconciliation, "evaluate_fresh_dependencies", return_value=evaluation
        ):
            assert recover() is None
    else:
        assert recover() is None
    assert "status:queued" not in world.labels()


def test_conflicting_footprint_preserves_recompute_marker(tmp_path):
    world, _, recover = _recovery_snapshot(tmp_path)
    assert recover({"dependent"}) is None
    assert RECOMPUTE in world.labels()
    assert not world.boundary.history


def test_recompute_preview_uses_snapshot_without_mutation(tmp_path):
    world, ctx, recover = _recovery_snapshot(tmp_path, apply=False)
    world.revoke(11)
    world.set_t_label(BASE_RED, True)
    before = dict(world.forge.issues)
    with patch.object(
        world.boundary,
        "get_issue",
        side_effect=AssertionError("no fresh preview reads"),
    ):
        assert recover() is not None
    assert world.forge.issues == before
    assert ctx.task(DEPENDENT).status_labels == ("status:blocked", RECOMPUTE)
    assert not world.boundary.history


def test_recompute_preview_is_reported_once_without_a_transition(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    observation = world.cycle(apply=False)
    assert observation.promotion_issue_numbers == (DEPENDENT,)
    assert not observation.promoted
    assert world.labels() == {"status:blocked", RECOMPUTE}
