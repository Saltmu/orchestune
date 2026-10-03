"""#820: the GitHub-backed retry budget for confirmed integration timeouts."""

from __future__ import annotations

from typing import Any

import pytest

from orchestune.infra.execution_deadline import (
    ExecutionDeadlineExceeded,
    ExecutionScope,
)
from orchestune.integrator.timeout_policy import (
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_UNKNOWN,
    ExecutionFailureCause,
    IntegrationExecutionPolicy,
)
from orchestune.integrator.timeout_retry import (
    EVENT_FINISHED,
    EVENT_RESERVED,
    EVENT_RESET,
    EVENT_TERMINAL,
    MARKER,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    BudgetReadError,
    BudgetVerdict,
    EventWriteUnconfirmed,
    ExecutionBudgetStore,
    ExecutionEvent,
    Target,
    evaluate_history,
    parse_event,
    planned_retry,
    reset_event,
)

PARENT = 100
NOW = 1_700_000_000.0
CI_TIMEOUT = ExecutionFailureCause.CI_TIMEOUT.value
POLICY = IntegrationExecutionPolicy()
TARGET = Target(7, "task-7", "a" * 40)


def _iso(offset: float = 0.0) -> str:
    from orchestune.integrator.timeout_retry import _iso as iso

    return iso(NOW + offset)


def _event(
    event: str, attempt: str, generation: int = 1, **fields: Any
) -> ExecutionEvent:
    return ExecutionEvent(
        parent_issue_number=fields.pop("parent", PARENT),
        generation=generation,
        attempt_id=attempt,
        event=event,
        executed_at=_iso(),
        targets=(TARGET,),
        **fields,
    )


def _reserved(attempt: str, generation: int = 1) -> ExecutionEvent:
    return _event(EVENT_RESERVED, attempt, generation, stage="ci")


def _timeout(
    attempt: str,
    generation: int = 1,
    *,
    next_retry_at: str | None = None,
    stop: bool | None = True,
    rollback: bool | None = True,
    side_effect: str = SIDE_EFFECT_NONE,
    outcome: str = CI_TIMEOUT,
) -> ExecutionEvent:
    return _event(
        EVENT_FINISHED,
        attempt,
        generation,
        outcome=outcome,
        stage="ci",
        next_retry_at=next_retry_at or _iso(-1),
        stop_confirmed=stop,
        rollback_confirmed=rollback,
        side_effect_state=side_effect,
    )


def _finished(attempt: str, outcome: str, generation: int = 1) -> ExecutionEvent:
    return _event(
        EVENT_FINISHED,
        attempt,
        generation,
        outcome=outcome,
        stop_confirmed=True,
        rollback_confirmed=True,
    )


def _verdict(*events: ExecutionEvent, policy: IntegrationExecutionPolicy = POLICY):
    return evaluate_history(list(events), PARENT, policy, now=NOW)


def _timeouts(count: int) -> list[ExecutionEvent]:
    events: list[ExecutionEvent] = []
    for index in range(count):
        events += [_reserved(f"a{index}"), _timeout(f"a{index}")]
    return events


class TestEventCodec:
    def test_render_and_parse_round_trip(self) -> None:
        event = _timeout("attempt-1", next_retry_at=_iso(60))

        body = event.render()

        assert body.startswith(MARKER)
        assert parse_event(body) == event

    def test_comment_without_the_marker_is_not_an_event(self) -> None:
        assert parse_event("looks like prose") is None

    @pytest.mark.parametrize(
        "body",
        [
            f"{MARKER}\nno payload",
            f"{MARKER}\n```json\n{{not json\n```",
            f'{MARKER}\n```json\n{{"event": "reserved"}}\n```',
        ],
    )
    def test_marked_but_malformed_comment_fails_closed(self, body: str) -> None:
        with pytest.raises(BudgetReadError):
            parse_event(body)

    def test_unknown_event_or_outcome_is_rejected(self) -> None:
        event = _reserved("a")
        for key, bad in (("event", "bogus"), ("outcome", "bogus")):
            payload = event.payload()
            payload[key] = bad
            import json

            body = f"{MARKER}\n```json\n{json.dumps(payload)}\n```"
            with pytest.raises(BudgetReadError):
                parse_event(body)


class TestBudgetFold:
    def test_empty_history_proceeds_in_generation_one(self) -> None:
        state = _verdict()

        assert state.verdict is BudgetVerdict.PROCEED
        assert (state.generation, state.timeouts) == (1, 0)

    def test_a_reservation_without_a_result_blocks(self) -> None:
        state = _verdict(_reserved("a0"))

        assert state.verdict is BudgetVerdict.HOLD
        assert state.blocking_attempt_id == "a0"

    def test_normal_success_closes_the_generation(self) -> None:
        state = _verdict(
            *_timeouts(1), _reserved("ok"), _finished("ok", OUTCOME_SUCCESS)
        )

        assert state.verdict is BudgetVerdict.PROCEED
        assert (state.generation, state.timeouts) == (2, 0)

    def test_a_normal_nonzero_exit_neither_counts_nor_clears(self) -> None:
        state = _verdict(*_timeouts(1), _reserved("f"), _finished("f", OUTCOME_FAILED))

        assert state.verdict is BudgetVerdict.PROCEED
        assert (state.generation, state.timeouts) == (1, 1)

    def test_a_confirmed_timeout_backs_off_until_the_retry_time(self) -> None:
        events = [_reserved("a0"), _timeout("a0", next_retry_at=_iso(60))]

        state = _verdict(*events)

        assert state.verdict is BudgetVerdict.BACKOFF
        assert state.next_retry_at == _iso(60)
        assert evaluate_history(events, PARENT, POLICY, now=NOW + 61).verdict is (
            BudgetVerdict.PROCEED
        )

    def test_first_two_timeouts_retry_and_the_third_is_terminal(self) -> None:
        assert _verdict(*_timeouts(2)).verdict is BudgetVerdict.PROCEED
        exhausted = _verdict(*_timeouts(3))

        assert exhausted.verdict is BudgetVerdict.EXHAUSTED
        assert exhausted.timeouts == 3
        assert exhausted.terminal_recorded is False

    def test_a_recorded_terminal_event_is_exhausted_too(self) -> None:
        state = _verdict(
            *_timeouts(3), _event(EVENT_TERMINAL, "a2", outcome=CI_TIMEOUT)
        )

        assert state.verdict is BudgetVerdict.EXHAUSTED
        assert state.terminal_recorded is True

    def test_zero_retries_makes_the_first_timeout_terminal(self) -> None:
        policy = IntegrationExecutionPolicy(max_integration_timeout_retries=0)

        assert _verdict(*_timeouts(1), policy=policy).verdict is BudgetVerdict.EXHAUSTED

    def test_lowering_the_limit_does_not_erase_recorded_timeouts(self) -> None:
        generous = IntegrationExecutionPolicy(max_integration_timeout_retries=5)
        strict = IntegrationExecutionPolicy(max_integration_timeout_retries=2)

        assert _verdict(*_timeouts(3), policy=generous).verdict is BudgetVerdict.PROCEED
        assert _verdict(*_timeouts(3), policy=strict).verdict is BudgetVerdict.EXHAUSTED

    @pytest.mark.parametrize(
        "bad",
        [
            {"stop": False},
            {"stop": None},
            {"rollback": False},
            {"rollback": None},
            {"side_effect": SIDE_EFFECT_UNKNOWN},
        ],
    )
    def test_an_unconfirmed_stop_rollback_or_write_blocks_for_a_human(
        self, bad: dict[str, Any]
    ) -> None:
        state = _verdict(_reserved("a0"), _timeout("a0", **bad))

        assert state.verdict is BudgetVerdict.HOLD
        assert state.timeouts == 0

    @pytest.mark.parametrize(
        "outcome",
        [
            ExecutionFailureCause.CLEANUP_FAILED.value,
            ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE.value,
        ],
    )
    def test_cleanup_and_indeterminate_outcomes_block(self, outcome: str) -> None:
        state = _verdict(_reserved("a0"), _finished("a0", outcome))

        assert state.verdict is BudgetVerdict.HOLD
        assert state.last_cause == outcome

    def test_planned_retry_backs_off_60_then_120_and_stops_after_the_limit(
        self,
    ) -> None:
        first = planned_retry(POLICY, 0, now=NOW)
        second = planned_retry(POLICY, 1, now=NOW)
        third = planned_retry(POLICY, 2, now=NOW)

        assert first == (True, _iso(60))
        assert second == (True, _iso(120))
        assert third == (False, None)


class TestInvalidHistory:
    @pytest.mark.parametrize(
        "events",
        [
            [_reserved("a", generation=2)],
            [_event(EVENT_RESERVED, "a", parent=999, stage="ci")],
            [_timeout("never-reserved")],
            [_reserved("a"), _reserved("b")],
            [_reserved("a"), _timeout("a", generation=2)],
            [_reserved("a"), _timeout("b")],
        ],
    )
    def test_mismatched_generation_parent_or_attempt_is_indeterminate(
        self, events: list[ExecutionEvent]
    ) -> None:
        assert _verdict(*events).verdict is BudgetVerdict.INDETERMINATE

    def test_an_identical_duplicate_event_is_ignored(self) -> None:
        reserved = _reserved("a0")

        state = _verdict(reserved, reserved, _timeout("a0"))

        assert state.timeouts == 1

    def test_a_conflicting_duplicate_event_is_indeterminate(self) -> None:
        state = _verdict(
            _reserved("a0"),
            _timeout("a0"),
            _timeout("a0", outcome=ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED.value),
        )

        assert state.verdict is BudgetVerdict.INDETERMINATE

    def test_a_reset_without_a_reason_is_indeterminate(self) -> None:
        reset = reset_event(
            PARENT,
            next_generation=2,
            references="a2",
            reason="",
            executed_at=_iso(),
            attempt_id="r1",
        )

        assert _verdict(*_timeouts(3), reset).verdict is BudgetVerdict.INDETERMINATE


class TestReset:
    def _reset(self, generation: int = 2, reason: str = "verified by an operator"):
        return reset_event(
            PARENT,
            next_generation=generation,
            references="a2",
            reason=reason,
            executed_at=_iso(),
            attempt_id="r1",
        )

    def test_an_explicit_reset_opens_a_new_generation_with_a_fresh_count(self) -> None:
        state = _verdict(*_timeouts(3), self._reset())

        assert state.verdict is BudgetVerdict.PROCEED
        assert (state.generation, state.timeouts) == (2, 0)

    def test_a_reset_releases_an_unconfirmed_attempt(self) -> None:
        state = _verdict(_reserved("a2"), self._reset())

        assert state.verdict is BudgetVerdict.PROCEED

    def test_a_reset_must_open_the_next_generation(self) -> None:
        assert _verdict(*_timeouts(3), self._reset(generation=5)).verdict is (
            BudgetVerdict.INDETERMINATE
        )

    def test_a_reset_with_nothing_to_release_is_invalid(self) -> None:
        assert _verdict(self._reset()).verdict is BudgetVerdict.INDETERMINATE

    def test_the_new_generation_counts_its_own_timeouts(self) -> None:
        state = _verdict(
            *_timeouts(3),
            self._reset(),
            _reserved("b0", generation=2),
            _timeout("b0", generation=2),
        )

        assert (state.generation, state.timeouts) == (2, 1)


class _CommentForge:
    """Issue-comment double with injectable read/write failures."""

    def __init__(self, login: str = "bot") -> None:
        self.login = login
        self.comments: list[dict[str, Any]] = []
        self.permissions: dict[str, str] = {}
        self.fail_reads = False
        self.read_failures_remaining = 0
        self.creates = 0
        self.mode = "ok"  # ok | lost-response | dropped | error

    def get_authenticated_user(self) -> str:
        return self.login

    def get_actor_permission(self, username: str) -> str:
        return self.permissions.get(username, "none")

    def list_all_issue_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        if self.fail_reads:
            raise RuntimeError("read failed")
        if self.read_failures_remaining > 0:
            self.read_failures_remaining -= 1
            raise RuntimeError("read failed")
        return [dict(c) for c in self.comments if c["issue"] == int(issue_number)]

    def add(self, body: str, login: str | None = None) -> None:
        self.comments.append(
            {"issue": PARENT, "body": body, "user": {"login": login or self.login}}
        )

    def create_issue_comment(
        self, issue_number: int | str, body: str
    ) -> dict[str, Any]:
        self.creates += 1
        if self.mode == "ok":
            self.add(body)
            return {"id": self.creates}
        if self.mode == "lost-response":
            self.add(body)
            raise TimeoutError("response lost")
        if self.mode == "dropped":
            return {"id": self.creates}  # acknowledged but never stored
        raise RuntimeError("write rejected")


def _store(forge: _CommentForge, **kwargs: Any) -> ExecutionBudgetStore:
    ids = iter(f"attempt-{n}" for n in range(1, 50))
    return ExecutionBudgetStore(
        forge,
        PARENT,
        kwargs.pop("policy", POLICY),
        clock=lambda: NOW,
        new_attempt_id=lambda: next(ids),
        **kwargs,
    )


class TestStoreReading:
    def test_the_budget_is_restored_from_github_by_a_brand_new_store(self) -> None:
        forge = _CommentForge()
        for event in _timeouts(2):
            forge.add(event.render())

        state = _store(forge).load()

        assert state.timeouts == 2
        assert state.verdict is BudgetVerdict.PROCEED

    def test_events_from_an_unrelated_author_are_ignored(self) -> None:
        forge = _CommentForge()
        for event in _timeouts(3):
            forge.add(event.render(), login="someone-else")

        assert _store(forge).load().timeouts == 0

    def test_a_forged_success_cannot_clear_the_budget(self) -> None:
        forge = _CommentForge()
        for event in _timeouts(3):
            forge.add(event.render())
        forge.add(_reserved("x", generation=1).render(), login="attacker")
        forge.add(_finished("x", OUTCOME_SUCCESS).render(), login="attacker")

        assert _store(forge).load().verdict is BudgetVerdict.EXHAUSTED

    def test_an_unattributed_comment_is_never_canonical(self) -> None:
        forge = _CommentForge()
        forge.comments.append({"issue": PARENT, "body": _reserved("a").render()})

        assert _store(forge).load().verdict is BudgetVerdict.PROCEED

    def test_a_reset_by_a_writer_is_accepted(self) -> None:
        forge = _CommentForge()
        forge.permissions["operator"] = "write"
        for event in _timeouts(3):
            forge.add(event.render())
        forge.add(
            reset_event(
                PARENT,
                next_generation=2,
                references="a2",
                reason="checked processes and refs",
                executed_at=_iso(),
                attempt_id="r1",
            ).render(),
            login="operator",
        )

        assert _store(forge).load().verdict is BudgetVerdict.PROCEED

    def test_a_reset_by_a_reader_is_ignored(self) -> None:
        forge = _CommentForge()
        forge.permissions["reader"] = "read"
        for event in _timeouts(3):
            forge.add(event.render())
        forge.add(
            reset_event(
                PARENT,
                next_generation=2,
                references="a2",
                reason="please",
                executed_at=_iso(),
                attempt_id="r1",
            ).render(),
            login="reader",
        )

        assert _store(forge).load().verdict is BudgetVerdict.EXHAUSTED

    def test_removing_a_label_is_not_a_reset(self) -> None:
        # The budget lives in comments, so there is nothing a label change could clear.
        forge = _CommentForge()
        for event in _timeouts(3):
            forge.add(event.render())

        assert _store(forge).load().verdict is BudgetVerdict.EXHAUSTED

    def test_an_unreadable_history_starts_nothing(self) -> None:
        forge = _CommentForge()
        forge.fail_reads = True

        state = _store(forge).load()

        assert state.verdict is BudgetVerdict.INDETERMINATE
        assert "could not be read" in state.reason

    def test_a_malformed_canonical_comment_from_the_executor_fails_closed(self) -> None:
        forge = _CommentForge()
        forge.add(f"{MARKER}\n```json\n{{broken\n```")

        assert _store(forge).load().verdict is BudgetVerdict.INDETERMINATE

    def test_an_unverifiable_executor_identity_fails_closed(self) -> None:
        forge = _CommentForge(login="")
        forge.add(_reserved("a").render(), login="x")

        assert _store(forge).load().verdict is BudgetVerdict.INDETERMINATE


class TestStoreWriting:
    def test_reserve_is_read_back_before_it_returns(self) -> None:
        forge = _CommentForge()
        store = _store(forge)

        reserved = store.reserve(store.load(), [TARGET], "dependency")

        assert reserved.attempt_id == "attempt-1"
        assert forge.creates == 1
        assert store.load().verdict is BudgetVerdict.HOLD  # reserved, unfinished

    def test_a_lost_response_is_rechecked_not_reposted(self) -> None:
        forge = _CommentForge()
        forge.mode = "lost-response"
        store = _store(forge)

        store.reserve(store.load(), [TARGET], "ci")

        assert forge.creates == 1
        assert len(forge.comments) == 1

    def test_a_write_that_never_lands_is_reposted_then_given_up_after_three(
        self,
    ) -> None:
        forge = _CommentForge()
        forge.mode = "dropped"
        store = _store(forge)

        with pytest.raises(EventWriteUnconfirmed):
            store.reserve(store.load(), [TARGET], "ci")

        assert forge.creates == 3
        assert forge.comments == []

    def test_a_rejected_write_is_unconfirmed(self) -> None:
        forge = _CommentForge()
        forge.mode = "error"
        store = _store(forge)

        with pytest.raises(EventWriteUnconfirmed):
            store.reserve(store.load(), [TARGET], "ci")

    def test_when_the_readback_fails_the_event_is_not_posted_again(self) -> None:
        forge = _CommentForge()
        store = _store(forge)
        state = store.load()
        forge.read_failures_remaining = 10

        with pytest.raises(EventWriteUnconfirmed):
            store.reserve(state, [TARGET], "ci")

        assert forge.creates == 1

    def test_a_transient_readback_failure_recovers_without_a_duplicate(self) -> None:
        forge = _CommentForge()
        store = _store(forge)
        state = store.load()
        forge.read_failures_remaining = 1

        store.reserve(state, [TARGET], "ci")

        assert forge.creates == 1
        assert len(forge.comments) == 1

    def test_no_event_is_written_once_the_deadline_has_passed(self) -> None:
        forge = _CommentForge()
        scope = ExecutionScope(cycle_seconds=1, cleanup_seconds=5, command_seconds=1)
        scope.started_at -= 10
        store = _store(forge, scope=scope)

        with pytest.raises(ExecutionDeadlineExceeded):
            store.reserve(store.load(), [TARGET], "ci")

        assert forge.creates == 0

    def test_after_the_deadline_the_cleanup_budget_still_allows_the_record(
        self,
    ) -> None:
        forge = _CommentForge()
        scope = ExecutionScope(cycle_seconds=1, cleanup_seconds=5, command_seconds=1)
        store = _store(forge, scope=scope)
        reserved = store.reserve(store.load(), [TARGET], "ci")
        scope.started_at -= 10

        with scope.cleanup_phase():
            store.finish(
                reserved,
                CI_TIMEOUT,
                targets=[TARGET],
                stage="ci",
                stop_confirmed=True,
                rollback_confirmed=True,
                side_effect_state=SIDE_EFFECT_NONE,
                next_retry_at=_iso(60),
            )

        assert store.load().timeouts == 1

    def test_terminal_for_history_records_a_missing_terminal_once(self) -> None:
        forge = _CommentForge()
        for event in _timeouts(3):
            forge.add(event.render())
        store = _store(forge)
        state = store.load()

        assert store.terminal_for_history(state) is not None
        assert store.terminal_for_history(store.load()) is None
        assert store.load().terminal_recorded is True


def test_reset_event_uses_the_canonical_marker_and_reason() -> None:
    event = reset_event(
        PARENT,
        next_generation=2,
        references="a2",
        reason="processes gone, refs checked",
        executed_at=_iso(),
        attempt_id="r1",
    )

    parsed = parse_event(event.render())

    assert parsed is not None
    assert parsed.event == EVENT_RESET
    assert parsed.reason == "processes gone, refs checked"
