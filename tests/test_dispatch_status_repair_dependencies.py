"""Issue #872: fresh status-repair dependency evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from orchestune.dispatch.dependency_assessment import DependencyState
from orchestune.dispatch.dependency_resolution import REASON_MISSING
from orchestune.dispatch.status_dependency_policy import dependencies_completed
from orchestune.dispatch.status_repair_dependencies import (
    evaluate_fresh_dependencies,
)
from orchestune.labels import StatusLabel
from tests.conftest import make_issue, make_task


@dataclass(frozen=True)
class _CompletionEvidence:
    confirmed: frozenset[int] = frozenset()

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return issue_number in self.confirmed


def _evaluate(subject, population, forge, *, confirmed=()):
    return evaluate_fresh_dependencies(
        subject,
        population,
        completion_evidence=_CompletionEvidence(frozenset(confirmed)),
        forge=forge,
    )


def test_missing_fresh_subject_fails_closed(in_memory_forge):
    stale = make_task(1, depends_on=())

    assert _evaluate(stale, {1: stale}, in_memory_forge) is None


def test_reparses_subject_and_uses_changed_dependency_declaration(in_memory_forge):
    stale = make_task(1, subtask_id="subject", depends_on=())
    dependency = make_task(2, subtask_id="dep", status_labels=(StatusLabel.DONE,))
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.DONE,))
    )

    result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)

    assert result is not None
    assert result.task.depends_on == ("dep",)
    assert result.dependencies.resolved == (2,)
    assert dependencies_completed(result.assessment)


def test_missing_dependency_in_fresh_population_stays_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("missing",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    result = _evaluate(stale, {1: stale}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == ()
    assert result.dependencies.unresolved[0].reason == REASON_MISSING
    assert not dependencies_completed(result.assessment)


def test_native_dependency_is_resolved_from_the_fresh_subject(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            blocked_by=(2,),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.NOT_NEEDED,))
    )

    result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == (2,)
    assert dependencies_completed(result.assessment)


def test_body_dependency_does_not_resolve_to_same_name_in_another_epic(
    in_memory_forge,
):
    stale = make_task(1, subtask_id="subject", parent_number=100)
    other_epic = make_task(2, subtask_id="dep", parent_number=200)
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
            parent={"number": 100},
        )
    )

    result = _evaluate(stale, {1: stale, 2: other_epic}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == ()
    assert not dependencies_completed(result.assessment)


def test_live_lifecycle_truth_table(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.DONE,))
    )

    for labels, expected in (
        ((StatusLabel.DONE,), True),
        ((StatusLabel.NOT_NEEDED,), True),
        ((StatusLabel.DONE, StatusLabel.QUEUED), False),
        ((StatusLabel.QUEUED,), False),
    ):
        in_memory_forge.seed_issue(make_issue(2, subtask_id="dep", labels=labels))
        result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)
        assert result is not None
        assert dependencies_completed(result.assessment) is expected


def test_confirmed_completion_overrides_live_labels_without_query(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    with patch.object(
        in_memory_forge,
        "get_issue_labels",
        wraps=in_memory_forge.get_issue_labels,
    ) as get_labels:
        result = _evaluate(
            stale,
            {1: stale, 2: dependency},
            in_memory_forge,
            confirmed={2},
        )

    assert result is not None
    assert result.assessment.resolved[0].state is DependencyState.COMPLETED
    get_labels.assert_not_called()


def test_forge_label_error_is_not_converted_to_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    with patch.object(
        in_memory_forge,
        "get_issue_labels",
        side_effect=RuntimeError("Forge down"),
    ):
        with pytest.raises(RuntimeError, match="Forge down"):
            _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)


def test_no_declared_dependencies_is_distinguishable_from_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject", depends_on=("stale",))
    in_memory_forge.seed_issue(
        make_issue(
            1, subtask_id="subject", depends_on=(), labels=(StatusLabel.BLOCKED,)
        )
    )

    result = _evaluate(stale, {1: stale}, in_memory_forge)

    assert result is not None
    assert result.dependencies.is_empty
    assert dependencies_completed(result.assessment)
