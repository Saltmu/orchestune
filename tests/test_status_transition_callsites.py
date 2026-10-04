"""Inventory and execution tests for every `transition_status_label` call site (#1217).

Static discovery keeps the registry honest (a new, unregistered call site fails
the build); the parameterized cases below are what verifies behaviour: each runs
the real production function against an in-memory forge that holds the case's
*actual* label set, then checks the adapter arguments, the resulting labels and
the classification against the status-machine policy.

Classification (see docs/*/status-labels.md):

* NORMAL    - exactly one lifecycle label is held; source != target; the pair is
              in `ALLOWED_TRANSITIONS`.
* SELF      - the held lifecycle label already is the target (a replay).
* INIT      - no lifecycle label is held; not a transition, so no source.
* REPAIR    - several lifecycle labels are held; the held set and the removal
              candidates are stated explicitly.
* AUXILIARY - an auxiliary label is held; it never takes part in the lifecycle
              table and its coexistence/removal is asserted separately.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestune.claim import preflight as claim_preflight
from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.repairs.status import plan_status_repairs
from orchestune.dispatch import launch_attempts
from orchestune.labels import StatusLabel
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.status_labels import (
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.ledger.status_machine import (
    LABEL_ROLES,
    LabelRole,
    is_allowed,
    lifecycle_labels,
)
from tests.conftest import FakeForge, make_issue
from tests.consistency_status_test_support import (
    _desired,
    _desired_task,
    _evaluate,
    _observed,
    _task_scope,
)
from tests.dispatch_test_support import make_test_dispatcher_config, make_test_task
from tests.status_transition_callsite_drivers import (
    DRIVERS,
    ISSUE,
    Env,
    completion_abandoned_finalize,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "orchestune"

Q = StatusLabel.QUEUED
B = StatusLabel.BLOCKED
P = StatusLabel.IN_PROGRESS
D = StatusLabel.DONE
N = StatusLabel.NOT_NEEDED
H = StatusLabel.BLOCKED_HUMAN_REVIEW
M = StatusLabel.MANUAL_MERGE_REQUIRED
RC = StatusLabel.BLOCKED_RECOMPUTE
FS = StatusLabel.FORCE_SERIAL
EL = StatusLabel.EXTERNAL_LOCK
CI_RED = "ci:base-branch-red"

#: Every call of `transition_status_label` under `orchestune/`:
#: "<file>::<enclosing function>" -> number of calls.
CALL_SITES: dict[str, int] = {
    "claim/service.py::_apply_status_label": 1,
    "dispatch/launch.py::_apply_yaml_error_blocking": 1,
    "dispatch/launch.py::_apply_invalid_footprint_blocking": 1,
    "dispatch/launch.py::_handle_launch_failure": 2,
    "dispatch/launch.py::_record_successful_launch": 1,
    "dispatch/launch_attempts.py::reconcile_attempt": 1,
    "dispatch/reconciliation.py::_resolve_one_blocked_recompute_issue": 1,
    "dispatch/reconciliation.py::_apply_base_branch_red_requeue": 1,
    "dispatch/rebase.py::notify_recompute": 1,
    "dispatch/rebase.py::_prepare_wip_backup_for_rebase": 1,
    "dispatch/rebase.py::_handle_rebase_failure": 1,
    "dispatch/gc/zombies.py::_notify_requeued_reclaim": 1,
    "dispatch/prior_parent_merge.py::_apply_verified_repair": 1,
    "dispatch/prior_parent_merge.py::_normalize_closed_issue_label": 1,
    "dispatch/gc/completion.py::_apply_blocked_hold": 1,
    "dispatch/gc/completion.py::_publish_requeue": 1,
    "dispatch/gc/completion.py::_apply_done_worktree_cleanup": 1,
    "dispatch/gc/cloud_completion.py::_handle_abandoned_cloud_reclaim": 1,
    "dispatch/status_repair.py::_apply_command": 1,
    "dispatch/recovery.py::execute_recovery_requeue_command": 1,
    "ledger/escalation.py::apply_human_review_escalation": 1,
    "integrator/steps.py::AutoMergeChildIntegrationStep._restore_blocked_label": 1,
}

#: Label paths that bypass the common adapter. They are recorded here, not covered
#: by the stateful guarantee: (file, function, why it is outside the adapter).
OUT_OF_SCOPE_PATHS: tuple[tuple[str, str, str], ...] = (
    (
        "complete/status_labels.py",
        "_completion_mutate",
        "`transition_completion_status_label`: separate adapter with generation checks",
    ),
    ("dispatch/gc/policy_effects.py", "reconcile_labels", "direct Forge add/remove"),
    (
        "replan/operations.py",
        "_transition_to_not_needed",
        "replan: add not-needed, remove status:*",
    ),
    (
        "integrator/pr.py",
        "handle_merge_failure",
        "rollback done -> queued, add then remove directly",
    ),
    (
        "dispatch/status_repair.py",
        "_apply_command",
        "COMMAND_ADD_LABEL / COMMAND_REMOVE_LABEL branch",
    ),
    (
        "dispatch/rebase.py",
        "notify_recompute",
        "adds the auxiliary blocked-recompute directly",
    ),
    (
        "dispatch/rebase.py",
        "_apply_forced_serial_event",
        "adds the auxiliary force-serial directly",
    ),
    (
        "dispatch/phase_rebase.py",
        "_apply_external_lock_sync",
        "auxiliary external-lock and queued re-add",
    ),
    (
        "dispatch/reconciliation.py",
        "_resolve_one_blocked_recompute_issue",
        "removes blocked-recompute directly",
    ),
    (
        "dispatch/reconciliation.py",
        "_apply_base_branch_red_unmark",
        "removes ci:base-branch-red",
    ),
    (
        "dispatch/reconciliation.py",
        "_apply_base_branch_red_escalate",
        "removes ci:base-branch-red",
    ),
    (
        "dispatch/prior_parent_merge.py",
        "_normalize_closed_issue_label",
        "terminal-label cleanup",
    ),
    (
        "dispatch/gc/completion.py",
        "_apply_escalated_base_branch_red",
        "removes ci:base-branch-red",
    ),
    (
        "dispatch/gc/completion.py",
        "_finalize_not_needed_worktree",
        "direct label removal",
    ),
)


_AUXILIARY_LABELS = frozenset(
    label for label, role in LABEL_ROLES.items() if role is LabelRole.AUXILIARY
)


class Kind(Enum):
    NORMAL = "normal"
    SELF = "self"
    INIT = "init"
    REPAIR = "repair"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True)
class Case:
    site: str
    driver: str
    condition: str
    held: tuple[str, ...]
    target: str
    old: tuple[str, ...]
    kind: Kind
    after: frozenset[str]
    callback: bool = False
    #: lifecycle labels the adapter legitimately leaves behind (REPAIR only).
    residual: frozenset[str] = frozenset()
    #: labels the call site removes itself, outside the common adapter.
    direct_removed: frozenset[str] = frozenset()
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        held = "+".join(label.removeprefix("status:") for label in self.held) or "none"
        target = self.target.removeprefix("status:")
        name = self.site.split("::")[1].split(".")[-1].strip("_")
        return f"{name}-{self.kind.value}-{held}-to-{target}-{self.condition}"


def _case(
    site: str,
    driver: str,
    condition: str,
    held: tuple[str, ...],
    target: str,
    old: tuple[str, ...],
    kind: Kind,
    after: Iterable[str],
    callback: bool = False,
    residual: Iterable[str] = (),
    direct_removed: Iterable[str] = (),
    **params: Any,
) -> Case:
    return Case(
        site=site,
        driver=driver,
        condition=condition,
        held=held,
        target=target,
        old=old,
        kind=kind,
        after=frozenset(after),
        callback=callback,
        residual=frozenset(residual),
        direct_removed=frozenset(direct_removed),
        params=params,
    )


NRM, SLF, INI, REP, AUX = (
    Kind.NORMAL,
    Kind.SELF,
    Kind.INIT,
    Kind.REPAIR,
    Kind.AUXILIARY,
)


def _for(site: str, driver: str) -> Callable[..., Case]:
    """`_case` bound to one call site and its driver."""
    return partial(_case, site, driver)


def _claim() -> list[Case]:
    c = _for("claim/service.py::_apply_status_label", "claim_apply_status_label")
    return [
        c("queued", (Q,), P, (Q,), NRM, {P}),
        c("blocked", (B,), P, (B,), NRM, {P}),
        c("aux-force-serial", (Q, FS), P, (Q, FS), AUX, {P}),
        c("aux-recompute-lock", (B, RC, EL), P, (B, RC, EL), AUX, {P}),
        c("no-lifecycle", (), P, (), INI, {P}),
        c("replay", (P,), P, (), SLF, {P}),
        c("queued-and-blocked", (Q, B), P, (Q, B), REP, {P}),
    ]


def _launch() -> list[Case]:
    yaml = _for("dispatch/launch.py::_apply_yaml_error_blocking", "launch_yaml_error")
    footprint = _for(
        "dispatch/launch.py::_apply_invalid_footprint_blocking",
        "launch_invalid_footprint",
    )
    fail = _for("dispatch/launch.py::_handle_launch_failure", "launch_failure")
    ok = _for("dispatch/launch.py::_record_successful_launch", "launch_success")
    return [
        yaml("queued", (Q,), B, (Q,), NRM, {B}),
        yaml("replay", (B,), B, (Q,), SLF, {B}),
        yaml("stale-both", (Q, B), B, (Q,), REP, {B}),
        footprint("queued", (Q,), H, (Q,), NRM, {H}),
        footprint("replay", (H,), H, (Q,), SLF, {H}),
        fail("claim-failed", (Q,), B, (Q,), NRM, {B}),
        fail("agent-failed", (P,), B, (Q, P), NRM, {B}, snapshot=(Q,), claimed=True),
        fail(
            "invalid-after-claim",
            (P,),
            H,
            (Q, P),
            NRM,
            {H},
            snapshot=(Q,),
            claimed=True,
            validation=True,
        ),
        fail("invalid", (Q,), H, (Q,), NRM, {H}, validation=True),
        fail("blocked-again", (B,), B, (B,), SLF, {B}),
        fail("aux-recompute", (Q, RC), B, (Q,), AUX, {B, RC}),
        ok("queued", (Q,), P, (Q,), NRM, {P}),
        ok("blocked", (B,), P, (B,), NRM, {P}),
        ok("queued-and-blocked", (Q, B), P, (Q, B), REP, {P}),
        ok("replay", (P,), P, (), SLF, {P}),
    ]


def _attempt_and_reconciliation() -> list[Case]:
    attempt = _for(
        "dispatch/launch_attempts.py::reconcile_attempt", "attempt_reconcile"
    )
    blocked = _for(
        "dispatch/reconciliation.py::_resolve_one_blocked_recompute_issue",
        "reconcile_blocked_recompute",
    )
    red = _for(
        "dispatch/reconciliation.py::_apply_base_branch_red_requeue",
        "reconcile_base_branch_red",
    )
    return [
        attempt("queued", (Q,), P, (Q,), NRM, {P}),
        attempt("blocked", (B,), P, (B,), NRM, {P}),
        attempt("replay", (P,), P, (P,), SLF, {P}),
        attempt("queued-and-blocked", (Q, B), P, (Q, B), REP, {P}),
        attempt("no-lifecycle", (), P, (), INI, {P}),
        attempt("aux-force-serial-kept", (Q, FS), P, (Q,), AUX, {P, FS}),
        blocked("blocked", (B,), Q, (B,), NRM, {Q}),
        blocked(
            "aux-recompute-removed", (B, RC), Q, (B,), AUX, {Q}, direct_removed={RC}
        ),
        blocked("replay", (Q,), Q, (B,), SLF, {Q}),
        red("blocked", (B, CI_RED), Q, (B,), NRM, {Q}, direct_removed={CI_RED}),
        red("replay", (Q,), Q, (B,), SLF, {Q}),
    ]


def _rebase_and_gc() -> list[Case]:
    notify = _for("dispatch/rebase.py::notify_recompute", "rebase_notify_recompute")
    wip = _for(
        "dispatch/rebase.py::_prepare_wip_backup_for_rebase",
        "rebase_wip_backup_failure",
    )
    fail = _for("dispatch/rebase.py::_handle_rebase_failure", "rebase_failure")
    zombie = _for("dispatch/gc/zombies.py::_notify_requeued_reclaim", "zombie_requeue")
    cases = [
        notify("aux-added-directly", (Q,), B, (Q,), AUX, {B, RC}),
        notify("aux-replay", (B, RC), B, (Q,), AUX, {B, RC}),
    ]
    for rebase_case in (wip, fail):
        cases += [
            rebase_case("in-progress", (P,), M, (P,), NRM, {M}),
            rebase_case("replay", (M,), M, (P,), SLF, {M}),
        ]
    return cases + [
        zombie("in-progress", (P,), Q, (P,), NRM, {Q}),
        zombie("blocked", (B,), Q, (B,), NRM, {Q}),
        zombie("both", (P, B), Q, (P, B), REP, {Q}, snapshot=(P, B)),
        zombie("stale-snapshot", (Q,), Q, (P,), SLF, {Q}, snapshot=(P,)),
    ]


def _prior_parent_and_completion() -> list[Case]:
    repair = _for(
        "dispatch/prior_parent_merge.py::_apply_verified_repair", "prior_parent_repair"
    )
    closed = _for(
        "dispatch/prior_parent_merge.py::_normalize_closed_issue_label",
        "prior_parent_normalize_closed",
    )
    hold = _for(
        "dispatch/gc/completion.py::_apply_blocked_hold", "completion_blocked_hold"
    )
    requeue = _for("dispatch/gc/completion.py::_publish_requeue", "completion_requeue")
    done = _for(
        "dispatch/gc/completion.py::_apply_done_worktree_cleanup",
        "completion_done_cleanup",
    )
    abandoned = _for(
        "dispatch/gc/cloud_completion.py::_handle_abandoned_cloud_reclaim",
        "completion_abandoned_reclaim",
    )
    return [
        repair("queued", (Q,), D, (Q,), NRM, {D}),
        repair("blocked", (B,), D, (B,), NRM, {D}),
        repair("in-progress", (P,), D, (P,), NRM, {D}),
        repair("queued-and-blocked", (Q, B), D, (Q, B), REP, {D}),
        repair("replay", (D,), D, (), SLF, {D}),
        closed("queued", (Q,), D, (Q,), NRM, {D}),
        closed("no-lifecycle", (), D, (), INI, {D}),
        hold("in-progress", (P,), B, (P,), NRM, {B}),
        hold("queued", (Q,), B, (Q,), NRM, {B}, snapshot=(Q,)),
        hold("aux-added-directly", (P,), B, (P,), AUX, {B, RC}, extra_label=RC),
        requeue("in-progress", (P,), Q, (P,), NRM, {Q}, True),
        requeue("blocked", (B,), Q, (B,), NRM, {Q}, True, snapshot=(B,)),
        requeue("replay", (Q,), Q, (P,), SLF, {Q}, True),
        done("in-progress", (P,), D, (P,), NRM, {D}),
        done("both", (Q, B), D, (Q, B), REP, {D}, snapshot=(Q, B)),
        abandoned("in-progress", (P,), Q, (P,), NRM, {Q}, True),
        abandoned("blocked", (B,), Q, (B,), NRM, {Q}, True),
    ]


def _repair_recovery_escalation() -> list[Case]:
    repair = _for("dispatch/status_repair.py::_apply_command", "status_repair_command")
    recovery = _for(
        "dispatch/recovery.py::execute_recovery_requeue_command", "recovery_requeue"
    )
    esc = _for("ledger/escalation.py::apply_human_review_escalation", "escalation")
    restore = _for(
        "integrator/steps.py::AutoMergeChildIntegrationStep._restore_blocked_label",
        "integrator_restore_blocked_label",
    )
    resolved = {"depends_on": ("external",), "completed": ("external",)}
    unresolved = {"depends_on": ("external",), "completed": ()}
    return [
        repair("dependency-resolved", (B,), Q, (B,), NRM, {Q}, True, **resolved),
        repair("dependency-unresolved", (Q,), B, (Q,), NRM, {B}, True, **unresolved),
        recovery("in-progress", (P,), Q, (P,), NRM, {Q}),
        recovery("also-blocked", (P, B), Q, (P, B), REP, {Q}),
        recovery("aux-force-serial-kept", (P, FS), Q, (P,), AUX, {Q, FS}),
        esc("in-progress", (P,), H, (P,), NRM, {H}, True, current=(P,)),
        esc("queued", (Q,), H, (Q,), NRM, {H}, True, current=(Q,)),
        esc("blocked", (B,), H, (B,), NRM, {H}, True, current=(B,)),
        esc("not-needed-timeout", (N,), H, (N,), NRM, {H}, True, current=(N,)),
        esc(
            "in-progress-and-queued", (P, Q), H, (P, Q), REP, {H}, True, current=(P, Q)
        ),
        esc("replay", (H,), H, (), SLF, {H}, True, current=()),
        esc("no-lifecycle", (), H, (), INI, {H}, True, current=()),
        restore("in-progress", (P,), H, (P,), NRM, {H}, current=(P,)),
        restore("queued-and-blocked", (Q, B), H, (Q, B), REP, {H}, current=(Q, B)),
    ]


CASES: tuple[Case, ...] = tuple(
    [
        *_claim(),
        *_launch(),
        *_attempt_and_reconciliation(),
        *_rebase_and_gc(),
        *_prior_parent_and_completion(),
        *_repair_recovery_escalation(),
    ]
)


def _enclosing_calls(path: Path) -> Iterator[str]:
    """Yield "<file>::<qualified enclosing function>" for each adapter call."""
    relative = path.relative_to(PACKAGE_ROOT).as_posix()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.found: list[str] = []

        def _scoped(self, node: ast.AST, name: str) -> None:
            self.scope.append(name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scoped(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scoped(node, node.name)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "transition_status_label":
                self.found.append(f"{relative}::{'.'.join(self.scope)}")
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    yield from visitor.found


def _discovered_call_sites() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for site in _enclosing_calls(path):
            counts[site] = counts.get(site, 0) + 1
    return counts


class TestRegistryMatchesTheSource:
    def test_every_call_site_is_registered_with_its_call_count(self) -> None:
        discovered = _discovered_call_sites()
        unregistered = sorted(set(discovered) - set(CALL_SITES))
        stale = sorted(set(CALL_SITES) - set(discovered))
        assert not unregistered, (
            f"new transition_status_label call site(s) {unregistered}: register them "
            "in CALL_SITES and add executed cases for each"
        )
        assert not stale, f"registered call site(s) no longer exist: {stale}"
        assert discovered == CALL_SITES

    def test_every_registered_site_has_an_executed_case(self) -> None:
        assert {case.site for case in CASES} == set(CALL_SITES)

    def test_every_case_names_a_driver_and_every_driver_is_used(self) -> None:
        assert {case.driver for case in CASES} == set(DRIVERS)

    def test_case_ids_are_unique(self) -> None:
        ids = [case.id for case in CASES]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize(("file", "function", "reason"), OUT_OF_SCOPE_PATHS)
    def test_out_of_scope_paths_still_operate_labels_directly(
        self, file: str, function: str, reason: str
    ) -> None:
        source = (PACKAGE_ROOT / file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        bodies = [
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ]
        assert bodies, f"{file}::{function} no longer exists ({reason})"
        assert any(
            "add_label(" in body or "remove_label(" in body for body in bodies
        ), f"{file}::{function} no longer touches labels directly ({reason})"


def _derived_kind(case: Case) -> Kind:
    held_lifecycle = lifecycle_labels(case.held)
    if not held_lifecycle:
        return Kind.INIT
    if len(held_lifecycle) > 1:
        return Kind.REPAIR
    return Kind.SELF if case.target in held_lifecycle else Kind.NORMAL


def _spy(module_name: str, monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    module = importlib.import_module(module_name)
    real = module.transition_status_label
    calls: list[SimpleNamespace] = []

    def spy(
        forge: Any,
        issue: Any,
        new_label: str,
        old_labels: Any,
        on_label_added: Any = None,
    ) -> None:
        consumed: list[str] = []

        def tracked() -> Iterator[str]:
            for label in old_labels:
                consumed.append(label)
                yield label

        record = SimpleNamespace(
            target=new_label, old=consumed, callback=on_label_added is not None
        )
        calls.append(record)
        real(forge, issue, new_label, tracked(), on_label_added)

    monkeypatch.setattr(module, "transition_status_label", spy)
    return calls


def _run(
    case: Case, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[SimpleNamespace], FakeForge]:
    forge = FakeForge()
    forge.issues[ISSUE] = make_issue(ISSUE, labels=case.held)
    config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
    module_name = "orchestune." + case.site.split("::")[0].removesuffix(".py").replace(
        "/", "."
    )
    calls = _spy(module_name, monkeypatch)
    DRIVERS[case.driver](
        Env(forge, config, monkeypatch, tmp_path, case.held, case.params)
    )
    return calls, forge


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_call_site_applies_the_expected_transition(
    case: Case, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, forge = _run(case, monkeypatch, tmp_path)

    # The production code reached the common adapter exactly once, with the
    # target and (lazily consumed) removal candidates this case documents.
    assert len(calls) == 1, calls
    assert calls[0].target == case.target
    assert tuple(calls[0].old) == case.old
    assert calls[0].callback is case.callback

    after = set(forge.get_issue_labels(ISSUE))
    assert after == set(case.after)

    held_lifecycle = lifecycle_labels(case.held)
    after_lifecycle = lifecycle_labels(after)
    derived = _derived_kind(case)
    if case.kind is Kind.AUXILIARY:
        assert derived in {Kind.NORMAL, Kind.SELF}
        touched = {*case.held, *after}
        assert touched & _AUXILIARY_LABELS
    else:
        assert derived is case.kind

    # The adapter never removes a label that was not offered (the call site's own
    # direct removals are declared), and leaves the target in place.
    assert set(case.held) - set(case.old) - case.direct_removed <= after
    assert case.target in after

    if derived in {Kind.NORMAL, Kind.SELF}:
        (source,) = held_lifecycle
        assert is_allowed(source, case.target), (source, case.target)
        if derived is Kind.NORMAL:
            assert (
                source in case.old
            ), "the held source must be among the removal candidates"
        assert after_lifecycle == {case.target}
    elif derived is Kind.INIT:
        assert not held_lifecycle
        assert after_lifecycle == {case.target}
    else:
        assert len(held_lifecycle) > 1
        assert case.residual <= held_lifecycle
        assert not case.residual & set(case.old)
        assert after_lifecycle == {case.target} | case.residual


def test_replaying_a_case_is_idempotent_for_label_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for case in CASES:
        forge = FakeForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=case.held)
        for _ in range(2):
            transition_status_label(forge, ISSUE, case.target, case.old)
        once = FakeForge()
        once.issues[ISSUE] = make_issue(ISSUE, labels=case.held)
        transition_status_label(once, ISSUE, case.target, case.old)
        assert set(forge.get_issue_labels(ISSUE)) == set(once.get_issue_labels(ISSUE))


def _env(
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    held: tuple[str, ...],
    **params: Any,
) -> Env:
    forge.issues[ISSUE] = make_issue(ISSUE, labels=held)
    config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
    return Env(forge, config, monkeypatch, tmp_path, held, params)


class TestEscalationProtectionStaysWithTheCallers:
    """The common adapter does not protect human-review labels; callers do."""

    def test_adapter_alone_adds_queued_beside_an_escalation_label(self) -> None:
        # A stale replay (old snapshot: in-progress) re-queues without looking at
        # the live labels. Phase 1 documents this and does not reject it.
        forge = FakeForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=(H,))
        assert not is_allowed(H, Q)

        transition_status_label(forge, ISSUE, Q, (P,))

        assert set(forge.get_issue_labels(ISSUE)) == {H, Q}

    @pytest.mark.parametrize("terminal", TERMINAL_ESCALATION_LABELS)
    def test_claim_preflight_rejects_terminal_escalation(
        self, terminal: StatusLabel
    ) -> None:
        failure = claim_preflight._check_status_labels(ISSUE, {Q, terminal})
        assert failure is not None
        assert failure.reason is claim_preflight.ClaimFailureReason.TERMINAL_ESCALATION

    @pytest.mark.parametrize("label", [*TERMINAL_ESCALATION_LABELS, D, N])
    def test_cloud_attempt_recovery_is_refused_for_terminal_labels(
        self, label: StatusLabel, tmp_path: Path
    ) -> None:
        forge = FakeForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=(P, label))
        config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
        task = make_test_task(ISSUE)
        assert launch_attempts._recovery_allowed(task, config) is False

    @pytest.mark.parametrize("terminal", TERMINAL_ESCALATION_LABELS)
    def test_recovery_requeue_skips_when_a_terminal_label_is_present(
        self, terminal: StatusLabel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env = _env(FakeForge(), monkeypatch, tmp_path, (P, terminal), expect="skipped")
        calls = _spy("orchestune.dispatch.recovery", monkeypatch)

        DRIVERS["recovery_requeue"](env)

        assert calls == []
        assert set(env.forge.get_issue_labels(ISSUE)) == {P, terminal}

    @pytest.mark.parametrize("terminal", TERMINAL_ESCALATION_LABELS)
    def test_abandoned_cloud_reclaim_keeps_status_labels_when_escalated(
        self, terminal: StatusLabel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env = _env(FakeForge(), monkeypatch, tmp_path, (terminal,))
        calls = _spy("orchestune.dispatch.gc.cloud_completion", monkeypatch)

        event = completion_abandoned_finalize(env)

        assert event.action == "abandoned_pr_requeued"
        assert calls == []
        assert set(env.forge.get_issue_labels(ISSUE)) == {terminal}

    def test_integrator_restore_does_not_touch_labels_when_already_escalated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env = _env(FakeForge(), monkeypatch, tmp_path, (H,), current=(H,))
        calls = _spy("orchestune.integrator.steps", monkeypatch)

        DRIVERS["integrator_restore_blocked_label"](env)

        assert calls == []

    @pytest.mark.parametrize("terminal", TERMINAL_ESCALATION_LABELS)
    def test_status_repair_planner_never_strips_a_human_gate(
        self, terminal: StatusLabel
    ) -> None:
        report = _evaluate(
            _observed(_task_scope(ISSUE, labels=(D, terminal))),
            _desired(
                _desired_task("status-policy", ISSUE, lifecycle=TaskLifecycle.DONE)
            ),
        )
        assert plan_status_repairs(report) == ()


class TestAdapterNeverCleansUpFinalOrEscalationLabels:
    """Removal candidates are explicit: the adapter never completes them itself."""

    @pytest.mark.parametrize("kept", [D, M])
    def test_human_review_escalation_leaves_final_and_other_escalation_labels(
        self, kept: StatusLabel
    ) -> None:
        forge = FakeForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=(P, kept))

        apply_human_review_escalation(ISSUE, (P, kept), "comment", forge=forge)

        assert set(forge.get_issue_labels(ISSUE)) == {H, kept}

    def test_unlisted_labels_survive_a_transition(self) -> None:
        forge = FakeForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=(Q, D, M, FS))

        transition_status_label(forge, ISSUE, P, (Q,))

        assert set(forge.get_issue_labels(ISSUE)) == {P, D, M, FS}
