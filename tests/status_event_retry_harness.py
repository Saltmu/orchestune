"""Stop/resume harness for the production retry paths (#1266, design #1219 §3).

Each world keeps only what survives a process stop - `run_state.json` on disk and
the Forge - and rebuilds everything else (the in-memory `RunState`, the reclaim
or completion context) on every run. A `Crash` is a `BaseException`, so the
production `except Exception` handlers cannot swallow it: it stops the run at a
named point exactly like a killed process.

Stop points (`StopPoint`):

* `reserve-save:before|after` - around the save that persists the reservation
  (`_record_reclaim`, `_publish_requeue`, the review-timeout policy `_prepare`);
* `add:before|after` - around adding the target label (`add:after` is also
  "before the settle callback");
* `settle-save:before|after` - around the save that confirms the reservation
  (`_settle_reclaim`, `_settle_completion_requeue`, `_apply_policy`);
* `remove:before|after` - around removing the old lifecycle label.

Only collaborators outside the budget decision are stubbed: worktree removal,
external-execution holds/stops and process liveness. The production budget
decisions (`_resolve_reclaim_count`, `plan_retry`, `exceeds_limit`) run unchanged;
the tests compare their effects with hand-written expectations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from orchestune.dispatch import gc as dispatch_gc
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import completion, policies, zombies
from orchestune.infra.process_utils import run_state_lock
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import (
    RunState,
    TaskReclaimRecord,
    load_run_state,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.ledger.status_events import RetryStates
from tests.conftest import FakeForge, make_issue
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_dispatcher_config,
    make_test_task,
)
from tests.status_event_test_support import NOW, _reclaim_state
from tests.status_transition_callsite_drivers import _NOOP, ISSUE, _fake

KEY = str(ISSUE)

#: Every point a run can stop at, in the order the production paths reach them.
STOP_POINTS: tuple[str, ...] = (
    "reserve-save:before",
    "reserve-save:after",
    "add:before",
    "add:after",
    "settle-save:before",
    "settle-save:after",
    "remove:before",
    "remove:after",
)


class Crash(BaseException):
    """The process stopped at a named point."""


@dataclass
class Stops:
    """The point at which the current run stops (consumed when reached)."""

    point: str | None = None
    reached: list[str] = field(default_factory=list)

    def check(self, point: str) -> None:
        self.reached.append(point)
        if point == self.point:
            self.point = None
            raise Crash(point)


class StoppingForge(FakeForge):
    """FakeForge that stops around label mutations and records added labels."""

    def __init__(self, stops: Stops) -> None:
        super().__init__()
        self.stops = stops
        self.added: list[str] = []

    def add_label(
        self, issue_number: int | str, label: str, actor: str = "bot"
    ) -> None:
        self.stops.check("add:before")
        super().add_label(issue_number, label, actor)
        self.added.append(label)
        self.stops.check("add:after")

    def remove_label(self, issue_number: int | str, label: str) -> None:
        self.stops.check("remove:before")
        super().remove_label(issue_number, label)
        self.stops.check("remove:after")


def stopping_save(stops: Stops, names: tuple[str, ...]) -> Callable[..., None]:
    """`save_run_state` that stops around its n-th call, named by `names[n]`.

    Calls past `names` reuse the last name. The real save runs in between, so a
    stop at `<name>:after` leaves the saved state on disk.
    """
    calls = 0

    def save(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        name = names[min(calls, len(names) - 1)]
        calls += 1
        stops.check(f"{name}:before")
        save_run_state(*args, **kwargs)
        stops.check(f"{name}:after")

    return save


def ledger_record(retries: RetryStates) -> TaskReclaimRecord:
    return TaskReclaimRecord(
        count=retries.reclaim.count,
        pending=retries.reclaim.pending,
        early_death_retry_count=retries.early_death.count,
        early_death_retry_at=retries.early_death.retry_at,
        early_death_retry_pending=retries.early_death.pending,
        review_timeout_retry_count=retries.review_timeout.count,
        review_timeout_retry_at=retries.review_timeout.retry_at,
        review_timeout_retry_pending=retries.review_timeout.pending,
        usage_limit_retry_count=retries.usage_limit.count,
        usage_limit_retry_at=retries.usage_limit.retry_at,
        usage_limit_retry_pending=retries.usage_limit.pending,
    )


@dataclass
class RetryWorld:
    """A persistent ledger and Forge for one Issue; runs rebuild the rest."""

    forge: StoppingForge
    config: DispatcherConfig
    monkeypatch: pytest.MonkeyPatch
    stops: Stops

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        labels: tuple[str, ...] = (StatusLabel.IN_PROGRESS,),
        retries: RetryStates | None = None,
        active: Any = None,
        **overrides: Any,
    ) -> RetryWorld:
        stops = Stops()
        forge = StoppingForge(stops)
        forge.issues[ISSUE] = make_issue(ISSUE, labels=labels)
        config = make_test_dispatcher_config(
            tmp_path, forge=forge, apply=True, **overrides
        )
        world = cls(forge, config, monkeypatch, stops)
        counts = {ISSUE: ledger_record(retries)} if retries is not None else {}
        state = RunState(active_worktrees={KEY: active}, task_reclaim_counts=counts)
        with world.locked():
            save_run_state(state, config.run_state_path)
        return world

    @contextmanager
    def locked(self) -> Iterator[None]:
        with run_state_lock(Path(self.config.run_state_path).with_suffix(".lock")):
            yield

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(self.forge.get_issue_labels(ISSUE))

    def ledger(self) -> RunState:
        return load_run_state_readonly(self.config.run_state_path)

    def retries(self) -> RetryStates:
        return _reclaim_state(self.ledger().task_reclaim_counts.get(ISSUE))

    def active_released(self) -> bool:
        return KEY not in self.ledger().active_worktrees

    def escalations(self) -> int:
        return self.forge.added.count(StatusLabel.BLOCKED_HUMAN_REVIEW)

    def run(self, run: Callable[[RunState], None], stop: str | None) -> bool:
        """Run once from the persisted ledger; False when it stopped at `stop`."""
        self.stops.point = stop
        try:
            with self.locked():
                run(load_run_state(self.config.run_state_path))
        except Crash:
            return False
        finally:
            self.stops.point = None
        return True


# ---- GC reclaim (`_record_reclaim` / `_settle_reclaim`) ----------------------


def reclaim_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    active = make_test_active_worktree(
        ISSUE, pid=None, worktree_path=str(tmp_path / "missing")
    )
    world = RetryWorld.create(tmp_path, monkeypatch, active=active, **kw)
    monkeypatch.setattr(zombies, "fresh_external_hold", lambda *a, **k: None)
    monkeypatch.setattr(
        zombies, "save_run_state", stopping_save(world.stops, ("reserve-save",))
    )
    return world


def gc_reclaim_once(world: RetryWorld, state: RunState, now: float = NOW) -> None:
    """One GC reclaim of the dead execution, if the ledger still holds it.

    `_settle_reclaim` is the second save of a counted reclaim; an already
    escalated Issue is not counted, so its only save is the settle save.
    """
    active = state.active_worktrees.get(KEY)
    if active is None:
        return
    labels = tuple(world.labels)
    escalated = StatusLabel.BLOCKED_HUMAN_REVIEW in labels
    names = ("settle-save",) if escalated else ("reserve-save", "settle-save")
    world.monkeypatch.setattr(
        zombies, "save_run_state", stopping_save(world.stops, names)
    )
    base = zombies.ZombieOrTimeoutReclaim(
        key=KEY,
        active=active,
        subtask_id="task-a",
        reason="zombie",
        finding_codes=(zombies.LOCAL_PROCESS_DEAD,),
        is_timeout=False,
        process_alive=False,
        status_labels=labels,
    )
    precondition = _fake(
        active=active, timed_out=False, process_alive=False, observed_at=now
    )
    reclaim = zombies._refresh_reclaim(state, base, world.config, precondition)
    zombies._reclaim_external_or_local(state, reclaim, world.config, None)


# ---- GC early death / review timeout (`_publish_requeue`) -------------------


def backoff_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> RetryWorld:
    active = make_test_active_worktree(
        ISSUE, started_at=NOW - 10, worktree_path=str(tmp_path / "missing")
    )
    world = RetryWorld.create(tmp_path, monkeypatch, active=active, **kw)
    monkeypatch.setattr(completion, "fresh_external_hold", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_gc, "fresh_external_hold", lambda *a, **k: None)
    for name in ("remove_worktree", "require_external_stop"):
        monkeypatch.setattr(completion, name, _NOOP)
    monkeypatch.setattr(
        completion, "save_run_state", stopping_save(world.stops, ("reserve-save",))
    )
    monkeypatch.setattr(
        dispatch_gc, "save_run_state", stopping_save(world.stops, ("settle-save",))
    )
    return world


def gc_backoff_once(
    world: RetryWorld, state: RunState, kind: str, now: float = NOW
) -> None:
    """`_handle_special_retry` with the GC's own settle callbacks."""
    active = state.active_worktrees.get(KEY)
    if active is None:
        return

    def settle(field_name: str) -> Callable[[], None]:
        return partial(
            dispatch_gc._settle_completion_requeue,
            state,
            world.config,
            KEY,
            ISSUE,
            field_name,
            (),
        )

    ctx = _fake(
        active=active,
        active_task=make_test_task(ISSUE, status_labels=tuple(world.labels)),
        config=world.config,
        run_state=state,
        now=now,
        open_prs=None,
        on_early_death_requeue=settle("early_death_retry_pending"),
        on_review_timeout_requeue=settle("review_timeout_retry_pending"),
    )
    action = (
        "completed_no_commits" if kind == "early_death" else "blocked_review_timeout"
    )
    completion._handle_special_retry(ctx, _fake(action=action))


# ---- review-timeout completion policy (`_prepare` / `_apply_policy`) --------


@dataclass
class PolicyWorld:
    """`tests.test_dispatch_gc_policies.policy_case` with stop points."""

    config: DispatcherConfig
    labels: list[str]
    comments: list[dict[str, Any]]
    stops: Stops

    @classmethod
    def create(
        cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **case: Any
    ) -> PolicyWorld:
        from tests.test_dispatch_gc_policies import policy_case

        _, config, forge, labels, comments = policy_case(tmp_path, **case)
        stops = Stops()
        add, remove = forge.add_label.side_effect, forge.remove_label.side_effect

        def stopping(op: str, apply: Callable[..., None]) -> Callable[..., None]:
            def run(*args: Any) -> None:
                stops.check(f"{op}:before")
                apply(*args)
                stops.check(f"{op}:after")

            return run

        forge.add_label.side_effect = stopping("add", add)
        forge.remove_label.side_effect = stopping("remove", remove)
        monkeypatch.setattr(
            policies,
            "save_run_state",
            stopping_save(stops, ("reserve-save", "settle-save")),
        )
        return cls(config, labels, comments, stops)

    def run(self, stop: str | None, now: float) -> bool:
        self.stops.point = stop
        try:
            policies.process_completion_policies(
                load_run_state_readonly(self.config.run_state_path),
                self.config,
                now=now,
            )
        except Crash:
            return False
        finally:
            self.stops.point = None
        return True

    def retry(self) -> TaskReclaimRecord | None:
        state = load_run_state_readonly(self.config.run_state_path)
        return state.task_reclaim_counts.get(250)
