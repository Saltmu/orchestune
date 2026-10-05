"""Generated real Supervisor/planner/executor recovery sequences.

Replay: pytest -n0 this file --hypothesis-seed=<seed>. Hypothesis prints the
shrunk initialize/rule sequence; retain it as a deterministic regression.
Printed blobs require a supported Hypothesis replay wrapper, not TestCase.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.models import ObservationCertainty
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
    assert not [f for f in final.report.findings if f.subject_id == "708"]
    assert not [c for c in final.repair_candidates if c.subject_id == "708"]
    assert lifecycle_labels(loop.labels()) == {target}
    labels = loop.labels()
    history = list(loop.boundary.history)
    journal = loop.journal().path.read_bytes() if loop.journal().path.exists() else b""
    for _ in range(2):
        _, final, _ = loop.cycle()
        assert not [c for c in final.repair_candidates if c.subject_id == "708"]
        assert loop.labels() == labels
        assert loop.boundary.history == history
        assert (
            loop.journal().path.read_bytes() if loop.journal().path.exists() else b""
        ) == journal


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
        if len(lifecycle_labels(self.loop.labels())) > 1 and any(
            ":status.add-label:" in i.operation for i in pending
        ):
            return  # removal cannot resume an add Intent after external relabeling
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
