"""Issue #868: `CycleContext`のsemantic query APIと内部所有権を固定する。

record系（`record_completion` / `record_launch` / `record_transition`）は
`tests/test_dispatch_cycle_context_records.py`が担当する。ここでは:

1. 内部所有——コンストラクタ入力への別名参照や返却値からの変更が新APIへ
   漏れないこと（Issue本文F段1・6）
2. `task` / `dependencies_of` / `assess_dependencies` / `has_changes_requested`
   / `is_ci_passed` / `canonical_branch` / `launch_fact`（F段2）
3. `is_effectively_done`と#867への委譲（F段3）
4. `queued_tasks` / `blocked_tasks`（F段4）

を検証する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_assessment import DependencyState
from orchestune.dispatch.dependency_resolution import (
    REASON_MISSING,
    TaskDependencies,
    UnresolvedDependency,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.labels import StatusLabel
from orchestune.models import Task

_TMP = Path(tempfile.mkdtemp(prefix="orchestune-test-cycle-context-api-"))


def _task(issue_number, **overrides):
    defaults = dict(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(StatusLabel.QUEUED,),
        created_at="2026-01-01T00:00:00Z",
        issue_state="OPEN",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _active(issue_number, **overrides):
    defaults = dict(
        issue_number=issue_number,
        branch=f"claude/issue-{issue_number}-task",
        worktree_path=f"worktrees/w{issue_number}",
        pid=1000 + issue_number,
        started_at=1_700_000_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=_TMP / "events.jsonl",
            run_state_path=_TMP / "run_state.json",
            worktree_root=_TMP / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class TestOwnership:
    """入力の別名参照・返却値からの変更が新APIへ漏れないこと（F段1・6）。"""

    def test_mutating_input_dict_after_construction_does_not_change_queries(self):
        tasks_by_issue = {1: _task(1, status_labels=(StatusLabel.QUEUED,))}
        ctx = _ctx(tasks_by_issue=tasks_by_issue)

        tasks_by_issue[1] = _task(1, status_labels=(StatusLabel.DONE,))
        tasks_by_issue[2] = _task(2)

        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)
        assert ctx.task(2) is None

    def test_mutating_input_sets_after_construction_does_not_change_queries(self):
        ci_passed = {1}
        changes_requested: set[int] = set()
        ctx = _ctx(
            tasks_by_issue={1: _task(1)},
            ci_passed_pr_issue_numbers=ci_passed,
            changes_requested_issue_numbers=changes_requested,
        )

        ci_passed.discard(1)
        ci_passed.add(2)
        changes_requested.add(1)

        assert ctx.is_ci_passed(1) is True
        assert ctx.is_ci_passed(2) is False
        assert ctx.has_changes_requested(1) is False

    def test_mutating_active_worktree_after_construction_does_not_change_launch_fact(
        self,
    ):
        active = _active(1, branch="claude/issue-1-original")
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": active}),
        )

        active.branch = "claude/issue-1-mutated"

        assert ctx.launch_fact(1).branch == "claude/issue-1-original"

    def test_queued_tasks_return_value_is_a_tuple_and_not_backed_by_internal_dict(
        self,
    ):
        ctx = _ctx(tasks_by_issue={1: _task(1)})

        result = ctx.queued_tasks()
        with pytest.raises(AttributeError):
            result.append(_task(2))  # type: ignore[attr-defined]

        assert ctx.queued_tasks() == (ctx.task(1),)


class TestQueries:
    """`task` / `dependencies_of` / CI / branch / launch query（F段2）。"""

    def test_task_returns_none_for_unknown_issue(self):
        ctx = _ctx()
        assert ctx.task(999) is None

    def test_task_reflects_effective_labels_via_dataclasses_replace(self):
        base = _task(1, status_labels=(StatusLabel.QUEUED, "priority:high"))
        ctx = _ctx(tasks_by_issue={1: base})

        result = ctx.task(1)
        assert result.issue_number == 1
        assert set(result.status_labels) == {StatusLabel.QUEUED, "priority:high"}
        # 変更がなければ同一オブジェクトを返す(新たなdataclasses.replaceを
        # 挟まない)。
        assert ctx.task(1) is base

    def test_dependencies_of_distinguishes_unknown_from_no_dependencies(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1)},
            dependency_resolution={1: TaskDependencies()},
        )
        assert ctx.dependencies_of(1) == TaskDependencies()
        assert ctx.dependencies_of(999) is None

    def test_dependencies_of_preserves_missing_resolution_as_none(self):
        # タスクは既知でも解決結果が欠落している(pathologicalケース)場合、
        # 空TaskDependencies()へ縮退させずNoneのままにする。
        ctx = _ctx(tasks_by_issue={1: _task(1)}, dependency_resolution={})
        assert ctx.dependencies_of(1) is None

    def test_assess_dependencies_delegates_to_issue_867_and_returns_none_when_missing(
        self,
    ):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1),
                2: _task(2, status_labels=(StatusLabel.DONE,)),
            },
            dependency_resolution={1: TaskDependencies(resolved=(2,))},
        )
        assessment = ctx.assess_dependencies(1)
        assert assessment.resolved[0].issue_number == 2
        assert assessment.resolved[0].state == DependencyState.COMPLETED
        assert ctx.assess_dependencies(999) is None

    def test_assess_dependencies_keeps_unresolved_diagnostics(self):
        diagnostic = UnresolvedDependency(raw="task-x", reason=REASON_MISSING)
        ctx = _ctx(
            tasks_by_issue={1: _task(1)},
            dependency_resolution={1: TaskDependencies(unresolved=(diagnostic,))},
        )
        assessment = ctx.assess_dependencies(1)
        assert assessment.resolved == ()
        assert assessment.unresolved == (diagnostic,)

    def test_has_changes_requested_and_is_ci_passed_are_static_observations(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1), 2: _task(2)},
            ci_passed_pr_issue_numbers={1},
            changes_requested_issue_numbers={2},
        )
        assert ctx.is_ci_passed(1) is True
        assert ctx.is_ci_passed(2) is False
        assert ctx.has_changes_requested(2) is True
        assert ctx.has_changes_requested(1) is False
        assert ctx.is_ci_passed(999) is False
        assert ctx.has_changes_requested(999) is False

    def test_canonical_branch_prefers_launch_fact_over_branch_mapping(self):
        active = _active(1, branch="claude/issue-1-launched")
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": active}),
            branch_by_issue_number={1: "claude/issue-1-canonical-name-only"},
        )
        assert ctx.canonical_branch(1) == "claude/issue-1-launched"

    def test_canonical_branch_falls_back_to_branch_mapping_without_launch(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1)},
            branch_by_issue_number={1: "claude/issue-1-canonical-name-only"},
        )
        assert ctx.canonical_branch(1) == "claude/issue-1-canonical-name-only"

    def test_canonical_branch_is_none_when_unknown(self):
        ctx = _ctx()
        assert ctx.canonical_branch(1) is None

    def test_launch_fact_is_none_for_unlaunched_issue(self):
        ctx = _ctx(tasks_by_issue={1: _task(1)})
        assert ctx.launch_fact(1) is None

    def test_launch_fact_is_none_when_ambiguous_multiple_active_worktrees(self):
        # 同一Issueに複数のActiveWorktreeが観測される曖昧なケース。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={
                    "1a": _active(1, branch="claude/issue-1-a"),
                    "1b": _active(1, branch="claude/issue-1-b"),
                }
            ),
        )
        assert ctx.launch_fact(1) is None

    def test_launch_fact_is_none_when_launch_phase_is_prepared_or_unknown(self):
        for phase in ("prepared", "unknown"):
            ctx = _ctx(
                tasks_by_issue={1: _task(1)},
                run_state=RunState(
                    active_worktrees={"1": _active(1, launch_phase=phase)}
                ),
            )
            assert ctx.launch_fact(1) is None, phase

    def test_launch_fact_is_none_when_handle_is_missing(self):
        # #868レビュー対応: recoveryがジャーナルも一致するPRも見つけられず
        # handle無し(pid/external_idいずれも無い)で復元したActiveWorktree
        # (`recovery._build_restored_active_worktree`)を、誤って確定的な
        # LaunchFactへ昇格させない。record_launchが同じ入力をinvalid-launch
        # として拒否するのと矛盾させない。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={
                    "1": _active(1, pid=None, external_id=None, launch_phase=None)
                }
            ),
        )
        assert ctx.launch_fact(1) is None

    def test_handleless_launch_excludes_issue_from_candidate_views(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            run_state=RunState(
                active_worktrees={
                    "1": _active(1, pid=None, external_id=None, launch_phase=None)
                }
            ),
        )
        assert ctx.queued_tasks() == ()


class TestIsEffectivelyDone:
    """`is_effectively_done`の統合規則（F段3）。"""

    def test_done_label_is_effectively_done(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.DONE,))})
        assert ctx.is_effectively_done(1) is True

    def test_not_needed_label_is_effectively_done(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.NOT_NEEDED,))}
        )
        assert ctx.is_effectively_done(1) is True

    def test_done_plus_queued_is_not_effectively_done(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.DONE, StatusLabel.QUEUED))
            }
        )
        assert ctx.is_effectively_done(1) is False

    def test_human_review_is_not_effectively_done(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,))
            }
        )
        assert ctx.is_effectively_done(1) is False

    def test_verified_prior_merge_completion_overrides_stale_labels(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.DONE, StatusLabel.QUEUED))
            },
            prior_parent_merge_completed_issue_numbers=frozenset({1}),
        )
        assert ctx.is_effectively_done(1) is True

    def test_unknown_issue_with_verified_prior_merge_is_done_without_task(self):
        ctx = _ctx(prior_parent_merge_completed_issue_numbers=frozenset({7}))
        assert ctx.is_effectively_done(7) is True
        assert ctx.task(7) is None

    def test_unknown_issue_without_evidence_is_not_done(self):
        ctx = _ctx()
        assert ctx.is_effectively_done(999) is False

    def test_subtask_id_is_not_a_condition_for_completion(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, subtask_id="", status_labels=(StatusLabel.DONE,))
            }
        )
        assert ctx.is_effectively_done(1) is True


class TestCandidateViews:
    """`queued_tasks` / `blocked_tasks`（F段4）。"""

    def test_returns_issue_number_ascending_regardless_of_input_order(self):
        ctx = _ctx(
            tasks_by_issue={
                30: _task(30, status_labels=(StatusLabel.QUEUED,)),
                10: _task(10, status_labels=(StatusLabel.QUEUED,)),
                20: _task(20, status_labels=(StatusLabel.QUEUED,)),
            }
        )
        assert [t.issue_number for t in ctx.queued_tasks()] == [10, 20, 30]

    def test_excludes_multiple_primary_status_labels(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.QUEUED, StatusLabel.BLOCKED))
            }
        )
        assert ctx.queued_tasks() == ()
        assert ctx.blocked_tasks() == ()

    def test_excludes_no_primary_status_label(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=("priority:high",))})
        assert ctx.queued_tasks() == ()
        assert ctx.blocked_tasks() == ()

    def test_excludes_closed_issue_state(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.QUEUED,), issue_state="CLOSED")
            }
        )
        assert ctx.queued_tasks() == ()

    def test_excludes_issue_with_active_launch_even_if_labels_still_say_queued(self):
        # 起動情報とForgeラベルの観測を混同しない: labelは古いQUEUEDのままでも
        # 実効起動状態(launch_fact)が候補viewから除外する。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            run_state=RunState(
                active_worktrees={"1": _active(1, launch_phase="unknown")}
            ),
        )
        assert ctx.queued_tasks() == ()

    def test_auxiliary_labels_alone_do_not_apply_policy_filters(self):
        # force-serial等の補助ラベルだけでcandidate viewをさらに絞らない
        # (quota/actor等のpolicyフィルタは加えない)。
        ctx = _ctx(
            tasks_by_issue={
                1: _task(
                    1,
                    status_labels=(StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL),
                )
            }
        )
        assert [t.issue_number for t in ctx.queued_tasks()] == [1]

    def test_blocked_tasks_mirrors_queued_tasks_for_blocked_primary(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.BLOCKED,))})
        assert [t.issue_number for t in ctx.blocked_tasks()] == [1]
        assert ctx.queued_tasks() == ()

    def test_verified_completion_excludes_issue_even_with_stale_queued_label(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            prior_parent_merge_completed_issue_numbers=frozenset({1}),
        )
        assert ctx.queued_tasks() == ()
