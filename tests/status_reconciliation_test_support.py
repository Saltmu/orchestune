"""Real closed loop and fault boundary for reconciliation tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import status_invariants
from orchestune.consistency.models import ObservationCertainty
from orchestune.consistency.observation import ForgeSnapshot, ObservationCollector
from orchestune.consistency.repairs.status import plan_status_repairs
from orchestune.consistency.supervisor import (
    ConsistencyMode,
    ConsistencySupervisor,
    FunctionRepairPlanner,
)
from orchestune.consistency.vocabulary import FACT_FORGE_REACHABLE, FACT_ISSUE_LABELS
from orchestune.dispatch.status_repair import (
    execute_status_repair_command,
    reconcile_status_repair_intents,
    status_intent_journal_path,
    task_lifecycle,
)
from orchestune.labels import StatusLabel
from orchestune.ledger.status_machine import lifecycle_labels
from orchestune.models import Task
from tests.conftest import FakeForge, make_issue, make_task
from tests.test_consistency_status_repair import _config, _evidence

NOW = datetime(2026, 10, 5, tzinfo=UTC)
ALLOWLIST = ("status.add-label", "status.transition-label", "status.remove-label")


class FaultBoundary:
    """Wrap the shared FakeForge; failures occur before/after its side effect."""

    def __init__(self, forge: FakeForge) -> None:
        self.forge = forge
        self.armed: tuple[str, str] | None = None
        self.history: list[tuple[str, int, str]] = []
        self.reads: list[tuple[str, int]] = []
        self.snapshots: list[frozenset[str]] = []

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.forge, name)
        if name.startswith("get_issue"):

            def read(number):
                self.reads.append((name, int(number)))
                return value(number)

            return read
        return value

    def mutate(self, op: str, number: int | str, label: str) -> None:
        mode = None
        if self.armed is not None and self.armed[0] == op:
            _, mode = self.armed
            self.armed = None
        if mode == "before":
            raise OSError("injected before mutation")
        previous = lifecycle_labels(self.forge.get_issue_labels(number))
        getattr(self.forge, op + "_label")(number, label)
        current = frozenset(self.forge.get_issue_labels(number))
        self.history.append((op, int(number), label))
        self.snapshots.append(current)
        if previous or op == "add":
            assert lifecycle_labels(current), "repair deleted the final lifecycle"
        if mode == "after":
            raise OSError("injected lost response")

    def add_label(self, number: int | str, label: str) -> None:
        self.mutate("add", number, label)

    def remove_label(self, number: int | str, label: str) -> None:
        self.mutate("remove", number, label)


class ReconciliationLoop:
    def __init__(self, root: Path, forge: FakeForge | None = None) -> None:
        self.root = root
        self.forge = forge if forge is not None else FakeForge()
        self.boundary = FaultBoundary(self.forge)
        self.config = _config(root, self.boundary)
        self.certainty = ObservationCertainty.KNOWN
        self.fact_shape = "valid"
        self.forge_known = True
        self.declared = True
        self.resolved = True
        self.execution = False
        self.allowlist = ALLOWLIST
        self.tasks: dict[int, Task] = {}
        self.restart()
        self.relabel(("status:queued",))

    def restart(self) -> None:
        self.supervisor = ConsistencySupervisor(
            repository_id="reconciliation",
            engine=ConsistencyEngine(status_invariants()),
            repair_planners=(FunctionRepairPlanner(plan_status_repairs),),
        )

    def relabel(self, labels: tuple[str, ...], *, state: str = "OPEN") -> None:
        self.forge.seed_issue(
            make_issue(
                708,
                labels=labels,
                depends_on=("dep",) if self.declared else (),
                state=state,
            )
        )
        self.epoch = getattr(self, "epoch", 0) + 1

    def sync_tasks(self) -> None:
        dep_labels = ("status:done",) if self.resolved else ("status:queued",)
        self.forge.seed_issue(make_issue(709, labels=dep_labels, subtask_id="dep"))
        issue = self.forge.get_issue(708)
        self.tasks = {
            708: make_task(
                708,
                status_labels=issue.labels if issue else (),
                depends_on=("dep",) if self.declared else (),
            ),
            709: make_task(709, subtask_id="dep", status_labels=dep_labels),
        }

    def observe(self):
        self.sync_tasks()
        from orchestune.consistency.observation import ExecutionRecord

        executions = (
            (ExecutionRecord(issue_number=708, branch="task", pid=123),)
            if self.execution
            else ()
        )
        if self.fact_shape == "ambiguous":
            executions = (
                ExecutionRecord(issue_number=708, branch="one", pid=1),
                ExecutionRecord(issue_number=708, branch="two", pid=2),
            )
        snapshot = ObservationCollector(
            repository_id="reconciliation", clock=lambda: NOW
        ).collect(
            forge=ForgeSnapshot(
                issues=tuple(self.forge.issues.values()),
                fetched_at=NOW,
                issues_complete=True,
            ),
            executions=executions,
        )
        scopes = []
        for scope in snapshot.observations:
            facts = []
            for fact in scope.facts:
                if fact.name == FACT_FORGE_REACHABLE and not self.forge_known:
                    fact = replace(fact, certainty=ObservationCertainty.UNKNOWN)
                if scope.subject_id == "708" and fact.name == FACT_ISSUE_LABELS:
                    fact = replace(fact, certainty=self.certainty)
                    if self.fact_shape == "missing":
                        continue
                    if self.fact_shape == "malformed":
                        fact = replace(fact, value=123)
                    if self.fact_shape == "duplicate":
                        facts.append(fact)
                facts.append(fact)
            scopes.append(replace(scope, facts=tuple(facts)))
        return replace(snapshot, observations=tuple(scopes))

    def derive(self, observed):
        journal = IntentJournal(status_intent_journal_path(self.config))
        inputs = tuple(
            DesiredTaskInput(
                task_id=t.subtask_id,
                subject_id=str(t.issue_number),
                depends_on=t.depends_on,
                lifecycle=task_lifecycle(t.status_labels),
            )
            for t in self.tasks.values()
        )
        return derive_desired_repository_state(
            "reconciliation",
            inputs,
            completed_task_ids=("dep",) if self.resolved else (),
            active_task_ids=(self.tasks[708].subtask_id,) if self.execution else (),
            policy=DispatchPolicy(max_concurrent=2),
            intents=journal.pending(now=NOW),
            now=NOW,
        )

    def execute(self, command):
        return execute_status_repair_command(
            command,
            self.tasks,
            completion_evidence=_evidence((709,) if self.resolved else ()),
            config=self.config,
            now=NOW,
        )

    def cycle(self, *, max_passes: int = 1):
        reconcile_status_repair_intents(self.config, now=NOW)
        self.restart()
        initial = self.supervisor.full_scan("cycle", observer=self, deriver=self)
        final = self.supervisor.repair_until_stable(
            initial,
            observer=self,
            deriver=self,
            executor=self,
            allowlist=self.allowlist,
            max_passes=max_passes,
        )
        return initial, final, self.supervisor.cycle_report(mode=ConsistencyMode.REPAIR)

    def labels(self) -> frozenset[str]:
        return frozenset(self.forge.get_issue_labels(708))

    def journal(self):
        return IntentJournal(status_intent_journal_path(self.config))


def recovery_oracle(
    labels: frozenset[str], resolved: bool, declared: bool = True
) -> str | None:
    """Independent case table; None means a persistent manual/held interval."""
    primary = lifecycle_labels(labels)
    resolved = resolved or not declared
    active = {StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS}
    protected = primary - active
    target = StatusLabel.QUEUED if resolved else StatusLabel.BLOCKED
    if primary == {StatusLabel.DONE, StatusLabel.QUEUED}:
        target = StatusLabel.QUEUED if resolved else StatusLabel.BLOCKED
        if not resolved:
            return None
    elif protected:
        if StatusLabel.DONE in primary and StatusLabel.QUEUED in primary:
            return None
        if len(protected) != 1:
            return None
        target = next(iter(protected))
        if target == StatusLabel.MANUAL_MERGE_REQUIRED and len(primary) > 1:
            return None  # desired human-review differs from retained merge gate
    elif primary == {StatusLabel.IN_PROGRESS}:
        return None  # no execution; a manual finding persists
    if target == StatusLabel.QUEUED and labels & {
        "ci:base-branch-red",
        "status:blocked-recompute",
    }:
        return None
    if primary and target not in primary and len(primary) > 1:
        return None
    if primary == {StatusLabel.BLOCKED} and not declared:
        return None
    return target
