"""Generated real Supervisor/planner/executor recovery sequences.

Replay: pytest -n0 this file --hypothesis-seed=<seed>. Hypothesis prints the
shrunk initialize/rule sequence; retain it as a deterministic regression.
Printed blobs require a supported Hypothesis replay wrapper, not TestCase.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.models import ObservationCertainty, RepairStatus
from orchestune.dispatch import status_repair
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState, save_run_state
from orchestune.ledger.status_machine import (
    LABEL_ROLES,
    LIFECYCLE_ROLES,
    LabelRole,
    lifecycle_labels,
)
from tests.status_reconciliation_test_support import (
    NOW,
    ReconciliationLoop,
    recovery_oracle,
)
from tests.verification_contract_test_support import expect_violation, require

LIFECYCLE = tuple(
    label for label, role in LABEL_ROLES.items() if role in LIFECYCLE_ROLES
)
EXTRAS = tuple(
    label for label, role in LABEL_ROLES.items() if role is LabelRole.AUXILIARY
) + ("ordinary", "status:future", "ci:base-branch-red")
LABELS = st.lists(st.sampled_from((*LIFECYCLE, *EXTRAS)), max_size=14)


def assert_recovery(loop: ReconciliationLoop) -> None:
    """A recoverable case takes exactly one cycle, then two idle cycles."""
    target = recovery_oracle(loop.labels(), loop.resolved, loop.declared)
    if target is None:
        return
    loop.boundary.armed = None
    loop.certainty = ObservationCertainty.KNOWN
    loop.fact_shape = "valid"
    loop.forge_known = True
    loop.config.apply = True
    initial, final, _ = loop.cycle()
    require(
        "P2-RECOVERY-ONE-CYCLE",
        not [f for f in final.report.findings if f.subject_id == "708"]
        and not [c for c in final.repair_candidates if c.subject_id == "708"]
        and lifecycle_labels(loop.labels()) == {target},
        f"labels after the first cycle: {sorted(loop.labels())}, want {target}",
    )
    labels = loop.labels()
    history = list(loop.boundary.history)
    journal = loop.journal().path.read_bytes() if loop.journal().path.exists() else b""
    for _ in range(2):
        _, final, _ = loop.cycle()
        require(
            "P2-RECOVERY-STABLE",
            not [c for c in final.repair_candidates if c.subject_id == "708"]
            and loop.labels() == labels
            and loop.boundary.history == history
            and (
                loop.journal().path.read_bytes()
                if loop.journal().path.exists()
                else b""
            )
            == journal,
            "an idle cycle after recovery changed labels, history or the journal",
        )


class ReconciliationMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        # Workspace-local example directory, retained across restarts.
        root = Path(".orchestune/tmp")
        root.mkdir(parents=True, exist_ok=True)
        self.directory = TemporaryDirectory(prefix="reconciliation-1218-", dir=root)
        self.loop = ReconciliationLoop(Path(self.directory.name))

    @initialize(labels=LABELS)
    def start(self, labels):
        self.loop.relabel(tuple(labels))

    @rule()
    def cycle(self):
        before = self.loop.labels()
        old_count = len(self.loop.boundary.history)
        _, _, report = self.loop.cycle()
        keys = [
            r.command.idempotency_key for p in report.repair_passes for r in p.results
        ]
        assert len(keys) == len(set(keys))
        mutations = self.loop.boundary.history[old_count:]
        assert all(label in LIFECYCLE for _, _, label in mutations)
        assert self.loop.labels() - set(LIFECYCLE) == before - set(LIFECYCLE)
        if (
            not self.loop.forge_known
            or self.loop.certainty is not ObservationCertainty.KNOWN
            or self.loop.fact_shape != "valid"
        ):
            assert not [m for m in mutations if m[1] == 708]

    @rule(labels=LABELS)
    def external_relabel(self, labels):
        self.loop.relabel(tuple(labels))

    @rule(resolved=st.booleans(), declared=st.booleans())
    def change_dependencies(self, resolved, declared):
        self.loop.resolved = resolved
        self.loop.declared = declared
        self.loop.relabel(tuple(self.loop.labels()))

    @rule(
        certainty=st.sampled_from(tuple(ObservationCertainty)),
        shape=st.sampled_from(
            ("valid", "missing", "duplicate", "malformed", "ambiguous")
        ),
        forge_known=st.booleans(),
    )
    def change_certainty(self, certainty, shape, forge_known):
        self.loop.certainty, self.loop.fact_shape, self.loop.forge_known = (
            certainty,
            shape,
            forge_known,
        )

    @rule(
        op=st.sampled_from(("add", "remove")), mode=st.sampled_from(("before", "after"))
    )
    def fail_next(self, op, mode):
        self.loop.boundary.armed = op, mode

    @rule()
    def restart(self):
        self.loop.restart()

    @rule(
        apply=st.booleans(),
        allowed=st.booleans(),
        execution=st.booleans(),
        state=st.sampled_from(("OPEN", "CLOSED", "ABSENT")),
        reservation=st.booleans(),
    )
    def change_availability(self, apply, allowed, execution, state, reservation):
        self.loop.config.apply = apply
        self.loop.allowlist = (
            ("status.add-label", "status.transition-label", "status.remove-label")
            if allowed
            else ()
        )
        self.loop.execution = execution
        self.loop.relabel(
            tuple(self.loop.labels()), state="CLOSED" if state == "CLOSED" else "OPEN"
        )
        if state == "ABSENT":
            self.loop.forge.issues.pop(708)
        save_run_state(
            RunState(
                completion_reservations={
                    "repo::708": {"issue_number": 708, "stage": "reserved"}
                }
                if reservation
                else {}
            ),
            self.loop.config.run_state_path,
        )
        self.reservation = reservation

    @rule(method=st.sampled_from(("plan", "mark_applied", "mark_verified")))
    def stop_at_journal_boundary(self, method):
        original = getattr(IntentJournal, method)

        def stop(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            raise OSError("injected journal stop")

        with patch.object(IntentJournal, method, stop):
            self.cycle()

    @rule()
    def recover(self):
        issue = self.loop.forge.get_issue(708)
        if (
            issue is None
            or issue.state != "OPEN"
            or getattr(self, "reservation", False)
            or self.loop.execution
        ):
            return
        # A conflicting live intent belongs to the deferred interval.
        pending = self.loop.journal().pending(now=NOW)
        target = recovery_oracle(
            self.loop.labels(), self.loop.resolved, self.loop.declared
        )
        if pending and any(
            change.value != target for i in pending for change in i.expected_changes
        ):
            return
        if not lifecycle_labels(self.loop.labels()) and any(
            ":status.transition-label:" in i.operation for i in pending
        ):
            return  # external deletion requires an add; transition Intent cannot resume it
        labels = self.loop.labels()
        if len(lifecycle_labels(labels)) > 1 and any(
            ":status.add-label:" in i.operation for i in pending
        ):
            return  # removal cannot resume an add Intent after external relabeling
        if lifecycle_labels(labels) and any(
            ":status.add-label:" in i.operation
            and any(change.value not in labels for change in i.expected_changes)
            for i in pending
        ):
            # An add Intent planned before an external relabel (found by the
            # baseline run: start([]), stop at plan, external [blocked]) cannot
            # be resumed once a lifecycle label exists, and a live Intent has no
            # expiry; this is the documented non-resumable deferred interval.
            return
        self.loop.allowlist = (
            "status.add-label",
            "status.transition-label",
            "status.remove-label",
        )
        assert_recovery(self.loop)

    def teardown(self):
        self.directory.cleanup()


TestReconciliationMachine = ReconciliationMachine.TestCase


def test_ci_profile_applies_to_reconciliation_case():
    applied: Any = TestReconciliationMachine.settings
    assert settings.get_current_profile_name() == "ci"
    assert (applied.max_examples, applied.stateful_step_count) == (100, 30)
    assert applied.deadline is None and applied.print_blob


# ---- deterministic scenarios and controls (#1275) --------------------------------


def _loop(
    root: Path, labels: tuple[str, ...], *, resolved: bool = True
) -> ReconciliationLoop:
    loop = ReconciliationLoop(root)
    loop.resolved = resolved
    loop.relabel(labels)
    return loop


def _change_before_execution(loop: ReconciliationLoop, labels: tuple[str, ...]) -> None:
    """An external change lands between planning and the executor's fresh read."""
    execute = loop.execute

    def hooked(command):
        loop.relabel(labels)
        loop.sync_tasks()
        return execute(command)

    loop.execute = hooked  # type: ignore[method-assign]


def scenario_protected_status_survives_a_stale_plan(root: Path, protected: str) -> None:
    """P2-FINAL-ESCALATION-PROTECTED: a stale plan never re-activates a protected task."""
    loop = _loop(root, ("status:blocked",))
    _change_before_execution(loop, (protected,))
    loop.cycle()
    require(
        "P2-FINAL-ESCALATION-PROTECTED",
        loop.labels() == {protected},
        f"labels after the repair: {sorted(loop.labels())}",
    )


def scenario_promotion_hold_survives_a_stale_plan(root: Path) -> None:
    """P2-PROMOTION-HOLD: a hold that appears before execution blocks the promotion."""
    loop = _loop(root, ("status:blocked",))
    _change_before_execution(loop, ("status:blocked", "ci:base-branch-red"))
    loop.cycle()
    require(
        "P2-PROMOTION-HOLD",
        "status:queued" not in loop.labels(),
        f"promoted despite the hold: {sorted(loop.labels())}",
    )


def scenario_applied_means_live_verified(root: Path) -> None:
    """P2-LIVE-VERIFICATION: an applied result is backed by the live labels."""
    loop = _loop(root, ("status:blocked",))
    _, _, report = loop.cycle()
    results = [r for p in report.repair_passes for r in p.results]
    for result in results:
        require(
            "P2-LIVE-VERIFICATION",
            result.status is not RepairStatus.APPLIED
            or lifecycle_labels(loop.labels()) == {StatusLabel.QUEUED},
            f"reported {result.status} with labels {sorted(loop.labels())}",
        )


def scenario_recovery_in_exactly_one_cycle(root: Path) -> None:
    assert_recovery(_loop(root, ("status:blocked",)))


_PROTECTED = ["status:done", "status:blocked-human-review"]


@pytest.mark.parametrize("protected", _PROTECTED)
def test_protected_status_survives_a_stale_plan(tmp_path, protected):
    scenario_protected_status_survives_a_stale_plan(tmp_path, protected)


def test_promotion_hold_survives_a_stale_plan(tmp_path):
    scenario_promotion_hold_survives_a_stale_plan(tmp_path)


def test_recovery_takes_exactly_one_cycle(tmp_path):
    scenario_recovery_in_exactly_one_cycle(tmp_path)


def test_a_command_that_changes_nothing_is_not_reported_applied(tmp_path, monkeypatch):
    """The production live verification downgrades a no-op apply to SKIPPED."""
    monkeypatch.setattr(status_repair, "_apply_command", lambda *a, **k: None)
    scenario_applied_means_live_verified(tmp_path)


def _always_fresh(command, task, tasks_by_issue, completion_evidence, config):
    return status_repair.evaluate_fresh_dependencies(
        task,
        tasks_by_issue,
        completion_evidence=completion_evidence,
        forge=config.resolved_forge,
    )


@pytest.mark.parametrize("protected", _PROTECTED)
def test_control_fresh_guard_bypass_is_detected(tmp_path, monkeypatch, protected):
    scenario_protected_status_survives_a_stale_plan(tmp_path / "normal", protected)
    monkeypatch.setattr(status_repair, "_fresh_preconditions_hold", _always_fresh)
    with expect_violation("P2-FINAL-ESCALATION-PROTECTED"):
        scenario_protected_status_survives_a_stale_plan(tmp_path / "fault", protected)


def test_control_hold_guard_bypass_is_detected(tmp_path, monkeypatch):
    scenario_promotion_hold_survives_a_stale_plan(tmp_path / "normal")
    monkeypatch.setattr(status_repair, "PROMOTION_HOLD_LABELS", ())
    with expect_violation("P2-PROMOTION-HOLD"):
        scenario_promotion_hold_survives_a_stale_plan(tmp_path / "fault")


def test_control_event_only_repair_is_detected(tmp_path, monkeypatch):
    scenario_applied_means_live_verified(tmp_path / "normal")
    monkeypatch.setattr(status_repair, "_apply_command", lambda *a, **k: None)
    monkeypatch.setattr(
        status_repair,
        "_verified_status_labels",
        lambda number, expected, config: (expected,),
    )
    with expect_violation("P2-LIVE-VERIFICATION"):
        scenario_applied_means_live_verified(tmp_path / "fault")


def test_control_repair_disabled_is_detected(tmp_path, monkeypatch):
    scenario_recovery_in_exactly_one_cycle(tmp_path / "normal")
    monkeypatch.setattr(
        "tests.status_reconciliation_test_support.plan_status_repairs",
        lambda report: (),
    )
    with expect_violation("P2-RECOVERY-ONE-CYCLE"):
        scenario_recovery_in_exactly_one_cycle(tmp_path / "fault")


def test_control_repair_delayed_by_one_cycle_is_detected(tmp_path, monkeypatch):
    scenario_recovery_in_exactly_one_cycle(tmp_path / "normal")
    import tests.status_reconciliation_test_support as support

    real, calls = support.plan_status_repairs, []

    def delayed(report):
        calls.append(report)
        return () if len(calls) == 1 else real(report)

    monkeypatch.setattr(support, "plan_status_repairs", delayed)
    with expect_violation("P2-RECOVERY-ONE-CYCLE"):
        scenario_recovery_in_exactly_one_cycle(tmp_path / "fault")
