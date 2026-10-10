"""Event model / production conformance checks with contract ids (#1275).

``check_route_coverage`` is the static guarantee (every production source has
its routes, every route condition is executed by a case); ``check_event_conformance``
is the dynamic one (the production driver reaches the labels, completion,
execution and budgets the pure model predicts).  They are separate contracts:
a complete table says nothing about whether the production labels agree.

The module-level names ``route_for`` and ``apply_event`` are looked up at call
time so a control test can inject a mis-routed Event or a wrong model target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.ledger.status_events import (
    Applied,
    EventInput,
    NoOp,
    Stage,
    apply_event,
)
from tests.conftest import make_issue
from tests.dispatch_test_support import make_test_dispatcher_config
from tests.status_event_cases import CURRENT, EVENT_CASES, FAIL_BEFORE, EventCase
from tests.status_event_test_support import (
    EVENT_BY_SOURCE,
    LABEL_INVARIANT_BUDGETS,
    LABEL_INVARIANT_COMPLETIONS,
    CaseEnv,
    FaultyForge,
    production_limits,
    route_for,
)
from tests.status_transition_callsite_drivers import ISSUE
from tests.test_status_transition_callsites import CALL_SITES, CASES, OUT_OF_SCOPE_PATHS
from tests.verification_contract_test_support import require

LIMITS = production_limits()

__all__ = [
    "CURRENT",
    "check_event_conformance",
    "check_route_coverage",
]


def check_route_coverage() -> None:
    """P3A-ROUTE-COVERAGE: static completeness of the route table."""
    call_sites = set(CALL_SITES.keys())
    out_of_scope = {f"{f}::{fn}" for f, fn, _ in OUT_OF_SCOPE_PATHS}
    invariant = {
        f"{f}::{fn}"
        for f, fn, _ in (*LABEL_INVARIANT_COMPLETIONS, *LABEL_INVARIANT_BUDGETS)
    }
    expected = call_sites | out_of_scope | invariant
    require(
        "P3A-ROUTE-COVERAGE",
        set(EVENT_BY_SOURCE.keys()) == expected,
        f"sources differ by {sorted(set(EVENT_BY_SOURCE.keys()) ^ expected)}",
    )
    route_conditions = {
        (source, c)
        for source, routes in EVENT_BY_SOURCE.items()
        for r in routes
        for c in r.cases
    }
    covered = {(c.site, c.condition) for c in CASES} | {
        (c.source, c.condition) for c in EVENT_CASES
    }
    require(
        "P3A-ROUTE-COVERAGE",
        route_conditions == covered,
        f"route conditions differ by {sorted(route_conditions ^ covered)}",
    )


def check_event_conformance(
    case: EventCase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P3A-DYNAMIC-CONFORMANCE: the production driver agrees with ``apply_event``."""
    route = route_for(case.source, case.condition)
    forge = FaultyForge()
    forge.issues[ISSUE] = make_issue(ISSUE, labels=case.held)
    config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
    env = CaseEnv(forge, config, monkeypatch, tmp_path, case.held, case.params)
    observations = case.driver(env)
    require(
        "P3A-DYNAMIC-CONFORMANCE",
        len(observations) == len(case.steps),
        f"{len(observations)} observations for {len(case.steps)} steps",
    )

    state = case.initial()
    for step, obs in zip(case.steps, observations, strict=False):
        if step == FAIL_BEFORE:
            applied_state = state
            res_tag = None
        else:
            stop_after = step if isinstance(step, Stage) else None
            inp = EventInput(
                route.event, route.kind, stop_after=stop_after, **case.inputs
            )
            res = apply_event(state, inp, LIMITS)
            if isinstance(res, Applied):
                applied_state = res.state
                res_tag = "applied"
            elif isinstance(res, NoOp):
                applied_state = state
                res_tag = "noop"
            else:
                applied_state = state
                res_tag = "rejected"
            state = applied_state

        where = f"{case.id} step {step!r}"
        require(
            "P3A-DYNAMIC-CONFORMANCE",
            obs.labels == applied_state.labels,
            f"{where}: production {sorted(obs.labels)} vs model "
            f"{sorted(applied_state.labels)}",
        )
        if obs.result is not None:
            require("P3A-DYNAMIC-CONFORMANCE", obs.result == res_tag, where)
        if obs.completion is not None:
            require(
                "P3A-DYNAMIC-CONFORMANCE",
                obs.completion == applied_state.completion_done,
                where,
            )
        if obs.execution_active is not None:
            require(
                "P3A-DYNAMIC-CONFORMANCE",
                obs.execution_active == applied_state.execution_active,
                where,
            )
        if obs.retries is not None:
            require(
                "P3A-DYNAMIC-CONFORMANCE", obs.retries == applied_state.retries, where
            )
        if obs.counts is not None:
            require(
                "P3A-DYNAMIC-CONFORMANCE", obs.counts == applied_state.counts, where
            )
