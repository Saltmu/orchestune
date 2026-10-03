"""Durable, GitHub-backed retry budget for confirmed integration timeouts (#820).

The count of timeouts must survive a new runner, a new run id and a re-created
context, so it is not kept in local ``run_state``. Each attempt writes canonical event
comments on the *parent Issue* and the budget is rebuilt from every comment page:

``reserved``   written (and read back) before dependency preparation / CI starts
``finished``   the attempt's outcome plus the stop / rollback confirmations
``terminal``   the retry limit was reached; the parent goes to human review
``reset``      an operator-issued, reasoned release that opens a new generation

Reading, ordering and validation fail closed: an unreadable, conflicting or invalid
history is ``INDETERMINATE`` and nothing is started. A ``reserved`` attempt without a
result cannot prove its processes stopped, so it is never silently re-run. The count
is bound to the parent Issue and an explicit generation and is not cleared by a normal
non-zero CI exit, a new run id or a settings change; only a confirmed normal success
or an explicit ``reset`` closes a generation.

GitHub comments have no atomic compare-and-swap. Concurrent applies against one
parent from different hosts are therefore unsupported; a detected conflict stops.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from orchestune.bounded_limit import exceeds_limit
from orchestune.infra.execution_deadline import (
    ExecutionCommandTimeout,
    ExecutionDeadlineExceeded,
    ExecutionScope,
)
from orchestune.integrator.timeout_policy import (
    COUNTED_TIMEOUT_CAUSES,
    SIDE_EFFECT_NONE,
    ExecutionFailureCause,
    IntegrationExecutionPolicy,
)

MARKER = "<!-- orchestune:integration-execution:v1 -->"
ESCALATION_MARKER = "<!-- orchestune:integration-execution-escalation:v1 -->"
# A reset may release a hold that has no GitHub attempt behind it (for example when
# the result could not be saved) by naming the hold record instead of an attempt.
LOCAL_HOLD_REFERENCE_PREFIX = "local-hold:"

EVENT_RESERVED = "reserved"
EVENT_FINISHED = "finished"
EVENT_TERMINAL = "terminal"
EVENT_RESET = "reset"
_EVENTS = frozenset({EVENT_RESERVED, EVENT_FINISHED, EVENT_TERMINAL, EVENT_RESET})

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
_CAUSE_OUTCOMES = frozenset(cause.value for cause in ExecutionFailureCause)
_OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILED}) | _CAUSE_OUTCOMES

# Permissions that may issue a ``reset`` besides the authenticated executor.
_RESET_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_WRITE_VERIFY_ATTEMPTS = 3
_BLOCKING_OUTCOMES = frozenset(
    {
        ExecutionFailureCause.CLEANUP_FAILED.value,
        ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE.value,
    }
)

_EVENT_BODY = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class BudgetReadError(RuntimeError):
    """The budget history could not be read or is invalid: start nothing."""


class EventWriteUnconfirmed(RuntimeError):
    """An event could not be confirmed on GitHub within its attempts or the deadline."""


class IssueCommentForge(Protocol):
    def list_all_issue_comments(
        self, issue_number: int | str
    ) -> list[dict[str, Any]]: ...

    def create_issue_comment(
        self, issue_number: int | str, body: str
    ) -> dict[str, Any]: ...

    def get_authenticated_user(self) -> str: ...

    def get_actor_permission(self, username: str) -> str: ...


@dataclass(frozen=True)
class Target:
    """One child Issue / subtask / source SHA taking part in an attempt."""

    issue_number: int
    subtask_id: str
    source_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "subtask_id": self.subtask_id,
            "source_sha": self.source_sha,
        }


@dataclass(frozen=True)
class ExecutionEvent:
    parent_issue_number: int
    generation: int
    attempt_id: str
    event: str
    executed_at: str
    targets: tuple[Target, ...] = ()
    outcome: str | None = None
    stage: str | None = None
    next_retry_at: str | None = None
    stop_confirmed: bool | None = None
    rollback_confirmed: bool | None = None
    side_effect_state: str = SIDE_EFFECT_NONE
    reason: str | None = None
    references: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "parent_issue_number": self.parent_issue_number,
            "generation": self.generation,
            "attempt_id": self.attempt_id,
            "event": self.event,
            "targets": [target.to_dict() for target in self.targets],
            "outcome": self.outcome,
            "stage": self.stage,
            "executed_at": self.executed_at,
            "next_retry_at": self.next_retry_at,
            "stop_confirmed": self.stop_confirmed,
            "rollback_confirmed": self.rollback_confirmed,
            "side_effect_state": self.side_effect_state,
            "reason": self.reason,
            "references": self.references,
        }

    def render(self) -> str:
        summary = (
            f"Integration execution `{self.event}` for parent #{self.parent_issue_number}"
            f" (generation {self.generation}, attempt `{self.attempt_id}`)"
        )
        body = json.dumps(self.payload(), indent=2, sort_keys=True)
        return f"{MARKER}\n{summary}\n\n```json\n{body}\n```"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BudgetReadError(message)


def _optional_bool(data: dict[str, Any], key: str) -> bool | None:
    value = data.get(key)
    _require(value is None or isinstance(value, bool), f"{key} must be a boolean")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    _require(value is None or isinstance(value, str), f"{key} must be a string")
    return value


def _parse_targets(raw: object) -> tuple[Target, ...]:
    if not isinstance(raw, list):
        raise BudgetReadError("targets must be a list")
    targets: list[Target] = []
    for item in raw:
        _require(isinstance(item, dict), "target must be an object")
        number = item.get("issue_number")
        subtask = item.get("subtask_id")
        sha = item.get("source_sha")
        _require(
            isinstance(number, int)
            and not isinstance(number, bool)
            and isinstance(subtask, str)
            and (sha is None or isinstance(sha, str)),
            "target has an invalid field",
        )
        targets.append(Target(number, subtask, sha))
    return tuple(targets)


def _load_payload(body: str) -> dict[str, Any]:
    match = _EVENT_BODY.search(body)
    if match is None:
        raise BudgetReadError("event comment has no JSON payload")
    try:
        data = json.loads(match.group(1))
    except ValueError as error:
        raise BudgetReadError(f"event payload is not valid JSON: {error}") from error
    _require(isinstance(data, dict), "event payload must be an object")
    return data  # type: ignore[no-any-return]


def _required_int(data: dict[str, Any], key: str, minimum: int | None = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetReadError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise BudgetReadError(f"{key} must be at least {minimum}")
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BudgetReadError(f"{key} must be a string")
    return value


def parse_event(body: str) -> ExecutionEvent | None:
    """Parse a canonical event; ``None`` for a comment that is not one.

    A comment that carries the marker but is malformed raises ``BudgetReadError`` so a
    trusted-but-broken history is never half-read.
    """
    if MARKER not in body:
        return None
    data = _load_payload(body)
    event = _required_str(data, "event")
    _require(event in _EVENTS, f"unknown event {event!r}")
    outcome = _optional_str(data, "outcome")
    _require(outcome is None or outcome in _OUTCOMES, f"unknown outcome {outcome!r}")
    side_effect = data.get("side_effect_state", SIDE_EFFECT_NONE)
    _require(isinstance(side_effect, str), "side_effect_state must be a string")
    return ExecutionEvent(
        parent_issue_number=_required_int(data, "parent_issue_number"),
        generation=_required_int(data, "generation", minimum=1),
        attempt_id=_required_str(data, "attempt_id"),
        event=event,
        executed_at=_required_str(data, "executed_at"),
        targets=_parse_targets(data.get("targets", [])),
        outcome=outcome,
        stage=_optional_str(data, "stage"),
        next_retry_at=_optional_str(data, "next_retry_at"),
        stop_confirmed=_optional_bool(data, "stop_confirmed"),
        rollback_confirmed=_optional_bool(data, "rollback_confirmed"),
        side_effect_state=side_effect,
        reason=_optional_str(data, "reason"),
        references=_optional_str(data, "references"),
    )


class BudgetVerdict(StrEnum):
    PROCEED = "proceed"
    BACKOFF = "backoff"
    EXHAUSTED = "exhausted"
    HOLD = "hold"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class BudgetState:
    verdict: BudgetVerdict
    generation: int = 1
    timeouts: int = 0
    next_retry_at: str | None = None
    reason: str = ""
    # The attempt that blocks (unresolved reservation, terminal, cleanup failure).
    blocking_attempt_id: str | None = None
    last_cause: str | None = None
    terminal_recorded: bool = False


def _wall_clock() -> float:
    """Wall-clock seconds; a seam so tests can move time without patching ``time``."""
    return time.time()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str) -> float:
    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    )


@dataclass
class _Fold:
    parent: int
    policy: IntegrationExecutionPolicy
    generation: int = 1
    timeouts: int = 0
    open_attempt: str | None = None
    blocked_by: str | None = None
    terminal_recorded: bool = False
    next_retry_at: str | None = None
    last_cause: str | None = None
    seen: dict[tuple[str, str], ExecutionEvent] = field(default_factory=dict)

    def apply(self, event: ExecutionEvent) -> None:
        _require(
            event.parent_issue_number == self.parent,
            f"event for parent #{event.parent_issue_number} found on #{self.parent}",
        )
        key = (event.attempt_id, event.event)
        previous = self.seen.get(key)
        if previous is not None:
            _require(previous == event, f"conflicting duplicate {key}")
            return
        self.seen[key] = event
        handler: dict[str, Callable[[ExecutionEvent], None]] = {
            EVENT_RESERVED: self._reserved,
            EVENT_FINISHED: self._finished,
            EVENT_TERMINAL: self._terminal,
            EVENT_RESET: self._reset,
        }
        handler[event.event](event)

    def _expect_generation(self, event: ExecutionEvent) -> None:
        _require(
            event.generation == self.generation,
            f"generation mismatch on {event.event} {event.attempt_id}: "
            f"{event.generation} != {self.generation}",
        )

    def _reserved(self, event: ExecutionEvent) -> None:
        self._expect_generation(event)
        _require(
            self.open_attempt is None and self.blocked_by is None,
            f"attempt {event.attempt_id} reserved while another is unresolved",
        )
        self.open_attempt = event.attempt_id

    def _finished(self, event: ExecutionEvent) -> None:
        self._expect_generation(event)
        _require(
            self.open_attempt == event.attempt_id,
            f"finished {event.attempt_id} has no matching reservation",
        )
        self.open_attempt = None
        outcome = event.outcome
        _require(outcome is not None, "finished event has no outcome")
        self.last_cause = outcome if outcome in _CAUSE_OUTCOMES else self.last_cause
        if outcome == OUTCOME_SUCCESS:
            self.generation += 1
            self.timeouts = 0
            self.next_retry_at = None
            self.last_cause = None
        elif outcome == OUTCOME_FAILED:
            self.next_retry_at = None
        elif outcome in {cause.value for cause in COUNTED_TIMEOUT_CAUSES}:
            confirmed = (
                event.stop_confirmed is True
                and event.rollback_confirmed is True
                and event.side_effect_state == SIDE_EFFECT_NONE
            )
            if confirmed:
                self.timeouts += 1
                self.next_retry_at = event.next_retry_at
            else:
                self.blocked_by = event.attempt_id
        else:
            self.blocked_by = event.attempt_id

    def _terminal(self, event: ExecutionEvent) -> None:
        self._expect_generation(event)
        _require(self.open_attempt is None, "terminal recorded with an open attempt")
        self.terminal_recorded = True
        self.blocked_by = self.blocked_by or event.attempt_id

    def _reset(self, event: ExecutionEvent) -> None:
        referenced = event.references
        _require(bool(event.reason), "reset requires a reason")
        if referenced is None:
            raise BudgetReadError("reset must reference an attempt")
        _require(
            event.generation == self.generation + 1,
            f"reset must open generation {self.generation + 1}",
        )
        if not referenced.startswith(LOCAL_HOLD_REFERENCE_PREFIX):
            unresolved = self.blocked_by or self.open_attempt
            exhausted = self.timeouts > self.policy.max_integration_timeout_retries
            _require(
                unresolved is not None or exhausted or self.terminal_recorded,
                "reset has nothing to release",
            )
            _require(
                referenced in {unresolved, *(k[0] for k in self.seen)},
                "reset references an unknown attempt",
            )
        self.generation = event.generation
        self.timeouts = 0
        self.open_attempt = None
        self.blocked_by = None
        self.terminal_recorded = False
        self.next_retry_at = None
        self.last_cause = None


def _state_of(
    fold: _Fold,
    verdict: BudgetVerdict,
    reason: str = "",
    *,
    blocking_attempt_id: str | None = None,
    next_retry_at: str | None = None,
) -> BudgetState:
    return BudgetState(
        verdict,
        generation=fold.generation,
        timeouts=fold.timeouts,
        next_retry_at=next_retry_at,
        reason=reason,
        blocking_attempt_id=blocking_attempt_id,
        last_cause=fold.last_cause,
        terminal_recorded=fold.terminal_recorded,
    )


def _verdict_of(fold: _Fold, now: float) -> BudgetState:
    policy = fold.policy
    if fold.open_attempt is not None:
        return _state_of(
            fold,
            BudgetVerdict.HOLD,
            f"attempt {fold.open_attempt} reserved without a recorded result; "
            "its processes cannot be proven stopped",
            blocking_attempt_id=fold.open_attempt,
        )
    exhausted = fold.timeouts >= policy.max_attempts
    if fold.blocked_by is not None and not (exhausted or fold.terminal_recorded):
        return _state_of(
            fold,
            BudgetVerdict.HOLD,
            f"attempt {fold.blocked_by} ended with an unconfirmed stop, rollback "
            "or write; a human must verify and issue a reset",
            blocking_attempt_id=fold.blocked_by,
        )
    if exhausted or fold.terminal_recorded:
        return _state_of(
            fold,
            BudgetVerdict.EXHAUSTED,
            f"{fold.timeouts} confirmed timeout(s) reached the retry limit",
            blocking_attempt_id=fold.blocked_by,
        )
    if fold.next_retry_at is not None and now < _epoch(fold.next_retry_at):
        return _state_of(
            fold,
            BudgetVerdict.BACKOFF,
            f"backing off until {fold.next_retry_at}",
            next_retry_at=fold.next_retry_at,
        )
    return _state_of(fold, BudgetVerdict.PROCEED)


def evaluate_history(
    events: Sequence[ExecutionEvent],
    parent_issue_number: int,
    policy: IntegrationExecutionPolicy,
    *,
    now: float,
) -> BudgetState:
    """Fold trusted, ordered events into a budget verdict. Fails closed."""
    fold = _Fold(parent_issue_number, policy)
    try:
        for event in events:
            fold.apply(event)
    except BudgetReadError as error:
        return BudgetState(BudgetVerdict.INDETERMINATE, reason=str(error))
    return _verdict_of(fold, now)


def planned_retry(
    policy: IntegrationExecutionPolicy, timeouts_before: int, *, now: float
) -> tuple[bool, str | None]:
    """Whether the timeout that just happened still allows a retry, and when.

    ``timeouts_before`` is the confirmed timeouts recorded before this one. The first
    retry waits ``backoff`` seconds, the second twice that (60 s, then 120 s by
    default); the timeout after the last allowed retry is terminal.
    """
    if exceeds_limit(timeouts_before + 1, policy.max_integration_timeout_retries):
        return False, None
    delay = policy.integration_timeout_backoff_seconds * 2**timeouts_before
    return True, _iso(now + delay)


class ExecutionBudgetStore:
    """Reads and writes the parent Issue's execution events for one parent."""

    def __init__(
        self,
        forge: IssueCommentForge,
        parent_issue_number: int,
        policy: IntegrationExecutionPolicy,
        *,
        scope: ExecutionScope | None = None,
        clock: Callable[[], float] | None = None,
        new_attempt_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.forge = forge
        self.parent = parent_issue_number
        self.policy = policy
        self.scope = scope
        self.clock = clock if clock is not None else _wall_clock
        self.new_attempt_id = new_attempt_id
        self._trusted_login: str | None = None

    # -- reading ---------------------------------------------------------

    def _executor_login(self) -> str:
        if self._trusted_login is None:
            try:
                login = self.forge.get_authenticated_user()
            except (
                Exception,
                ExecutionCommandTimeout,
                ExecutionDeadlineExceeded,
            ) as error:
                raise BudgetReadError(
                    f"the executing identity could not be read: {error}"
                ) from error
            if not isinstance(login, str) or not login:
                raise BudgetReadError("the executing identity could not be verified")
            self._trusted_login = login
        return self._trusted_login

    def _may_reset(self, login: str) -> bool:
        if login == self._executor_login():
            return True
        try:
            return str(self.forge.get_actor_permission(login)) in _RESET_PERMISSIONS
        except Exception as error:
            raise BudgetReadError(
                f"permission of reset author {login!r} could not be verified"
            ) from error

    def read_events(self) -> list[ExecutionEvent]:
        try:
            comments = self.forge.list_all_issue_comments(self.parent)
        except (Exception, ExecutionCommandTimeout, ExecutionDeadlineExceeded) as error:
            raise BudgetReadError(
                f"issue comments could not be read: {error}"
            ) from error
        if not isinstance(comments, list):
            raise BudgetReadError("issue comments response is not a list")
        events: list[ExecutionEvent] = []
        for comment in comments:
            if not isinstance(comment, dict):
                raise BudgetReadError("issue comment is not an object")
            body = comment.get("body")
            if not isinstance(body, str) or MARKER not in body:
                continue
            user = comment.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or not login:
                continue  # an unattributed comment is never canonical
            trusted_executor = login == self._executor_login()
            event = (
                parse_event(body) if trusted_executor else self._untrusted(body, login)
            )
            if event is not None:
                events.append(event)
        return events

    def _untrusted(self, body: str, login: str) -> ExecutionEvent | None:
        """Only a reasoned reset by a trusted operator is accepted from another author."""
        try:
            event = parse_event(body)
        except BudgetReadError:
            return None
        if event is None or event.event != EVENT_RESET:
            return None
        return event if self._may_reset(login) else None

    def load(self) -> BudgetState:
        try:
            events = self.read_events()
        except BudgetReadError as error:
            return BudgetState(BudgetVerdict.INDETERMINATE, reason=str(error))
        return evaluate_history(events, self.parent, self.policy, now=self.clock())

    # -- writing ---------------------------------------------------------

    def _present(self, wanted: ExecutionEvent) -> bool:
        return any(
            event.attempt_id == wanted.attempt_id and event.event == wanted.event
            for event in self.read_events()
        )

    def write_event(self, event: ExecutionEvent) -> None:
        """Post ``event`` and confirm it is readable; never trust the POST response.

        A lost response is rechecked under the same ``attempt_id`` instead of being
        reposted blindly. At most three confirmation attempts, all inside the deadline.
        """
        body = event.render()
        absent_proven = True
        for _ in range(_WRITE_VERIFY_ATTEMPTS):
            if self.scope is not None:
                self.scope.check("integration-event")
            if absent_proven:
                try:
                    self.forge.create_issue_comment(self.parent, body)
                except (Exception, ExecutionCommandTimeout, ExecutionDeadlineExceeded):
                    pass  # the response may have been lost; confirm below
            try:
                if self._present(event):
                    return
                absent_proven = True
            except BudgetReadError:
                absent_proven = False
        raise EventWriteUnconfirmed(
            f"{event.event} event {event.attempt_id} could not be confirmed on "
            f"#{self.parent}"
        )

    def _event(
        self, event: str, generation: int, attempt_id: str, **fields: Any
    ) -> ExecutionEvent:
        return ExecutionEvent(
            parent_issue_number=self.parent,
            generation=generation,
            attempt_id=attempt_id,
            event=event,
            executed_at=_iso(self.clock()),
            **fields,
        )

    def reserve(
        self, state: BudgetState, targets: Sequence[Target], stage: str
    ) -> ExecutionEvent:
        event = self._event(
            EVENT_RESERVED,
            state.generation,
            self.new_attempt_id(),
            targets=tuple(targets),
            stage=stage,
        )
        self.write_event(event)
        return event

    def finish(
        self,
        reserved: ExecutionEvent,
        outcome: str,
        *,
        targets: Sequence[Target],
        stage: str | None,
        stop_confirmed: bool | None,
        rollback_confirmed: bool | None,
        side_effect_state: str,
        next_retry_at: str | None = None,
    ) -> ExecutionEvent:
        event = self._event(
            EVENT_FINISHED,
            reserved.generation,
            reserved.attempt_id,
            targets=tuple(targets),
            outcome=outcome,
            stage=stage,
            next_retry_at=next_retry_at,
            stop_confirmed=stop_confirmed,
            rollback_confirmed=rollback_confirmed,
            side_effect_state=side_effect_state,
        )
        self.write_event(event)
        return event

    def terminal(
        self, reserved: ExecutionEvent, *, outcome: str, targets: Sequence[Target]
    ) -> ExecutionEvent:
        event = self._event(
            EVENT_TERMINAL,
            reserved.generation,
            reserved.attempt_id,
            targets=tuple(targets),
            outcome=outcome,
            stage=reserved.stage,
        )
        self.write_event(event)
        return event

    def terminal_for_history(self, state: BudgetState) -> ExecutionEvent | None:
        """Record a missing terminal event for an exhausted history (retry of a failed save)."""
        if state.terminal_recorded:
            return None
        attempt = state.blocking_attempt_id or self.new_attempt_id()
        event = self._event(
            EVENT_TERMINAL,
            state.generation,
            attempt,
            outcome=ExecutionFailureCause.RETRY_BUDGET_EXHAUSTED.value,
        )
        self.write_event(event)
        return event


def reset_event(
    parent_issue_number: int,
    *,
    next_generation: int,
    references: str,
    reason: str,
    executed_at: str,
    attempt_id: str,
) -> ExecutionEvent:
    """The canonical reset an operator posts after verifying processes, worktree and refs."""
    return ExecutionEvent(
        parent_issue_number=parent_issue_number,
        generation=next_generation,
        attempt_id=attempt_id,
        event=EVENT_RESET,
        executed_at=executed_at,
        reason=reason,
        references=references,
    )


__all__ = [
    "ESCALATION_MARKER",
    "EVENT_FINISHED",
    "EVENT_RESERVED",
    "EVENT_RESET",
    "EVENT_TERMINAL",
    "LOCAL_HOLD_REFERENCE_PREFIX",
    "MARKER",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCESS",
    "BudgetReadError",
    "BudgetState",
    "BudgetVerdict",
    "EventWriteUnconfirmed",
    "ExecutionBudgetStore",
    "ExecutionEvent",
    "IssueCommentForge",
    "Target",
    "evaluate_history",
    "parse_event",
    "planned_retry",
    "reset_event",
]
