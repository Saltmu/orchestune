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

    def test_nested_task_and_dependency_collections_are_owned(self):
        footprint = ["src/a.py"]
        candidates = [2, 3]
        resolved = [2]
        diagnostics = [
            UnresolvedDependency(
                raw="dep", reason=REASON_MISSING, candidates=candidates
            )
        ]
        ctx = _ctx(
            tasks_by_issue={1: _task(1, footprint=footprint)},
            dependency_resolution={
                1: TaskDependencies(resolved=resolved, unresolved=diagnostics)
            },
        )
        task = ctx.task(1)
        deps = ctx.dependencies_of(1)
        footprint.append("src/b.py")
        candidates.append(4)
        resolved.append(5)
        diagnostics.clear()
        assert task.footprint == ("src/a.py",)
        assert deps.resolved == (2,)
        assert deps.unresolved[0].candidates == (2, 3)
        assert ctx.dependencies_of(1) == deps

    def test_legacy_alias_updates_share_observations_but_not_record_deltas(self):
        original = {1: _task(1)}
        ctx = _ctx(tasks_by_issue=original)
        ctx.tasks_by_issue[1] = _task(1, status_labels=(StatusLabel.BLOCKED,))
        ctx.dependency_resolution[1] = TaskDependencies(resolved=(2,))
        ctx.ci_passed_pr_issue_numbers.add(1)
        ctx.changes_requested_issue_numbers.add(1)
        ctx.branch_by_issue_number[1] = "observed"
        assert original[1].status_labels == (StatusLabel.QUEUED,)
        assert ctx.task(1).status_labels == (StatusLabel.BLOCKED,)
        assert ctx.blocked_tasks() == (ctx.task(1),)
        assert ctx.dependencies_of(1).resolved == (2,)
        assert ctx.is_ci_passed(1)
        assert ctx.has_changes_requested(1)
        assert ctx.canonical_branch(1) == "observed"
        ctx.record_completion(1)
        assert ctx.tasks_by_issue[1].status_labels == (StatusLabel.BLOCKED,)
        ctx.tasks_by_issue[1] = _task(1)
        assert ctx.task(1).status_labels == (StatusLabel.DONE,)

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

    def test_orphan_observations_do_not_create_a_known_task(self):
        ctx = _ctx(
            dependency_resolution={9: TaskDependencies()},
            branch_by_issue_number={9: "orphan"},
            ci_passed_pr_issue_numbers={9},
            changes_requested_issue_numbers={9},
            run_state=RunState(active_worktrees={"9": _active(9)}),
        )
        assert ctx.task(9) is None
        assert ctx.dependencies_of(9) is None
        assert ctx.assess_dependencies(9) is None
        assert ctx.canonical_branch(9) is None
        assert ctx.launch_fact(9) is None
        assert not ctx.is_ci_passed(9)
        assert not ctx.has_changes_requested(9)

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

    def test_launch_fact_is_none_when_external_id_is_empty_string(self):
        # #868レビュー対応: `_parse_active_worktrees`は空文字列のexternal_id
        # をそのまま保持するため、`is not None`だけでは有効なhandleとして
        # 誤認する。空文字列はpid欠如と同じ「照会不能」として扱う。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={
                    "1": _active(1, pid=None, external_id="", launch_phase=None)
                }
            ),
        )
        assert ctx.launch_fact(1) is None

    def test_launch_fact_is_none_when_pid_is_not_a_usable_process_id(self):
        # #868レビュー対応: `_parse_active_worktrees`はpidを検証せず復元する
        # ため、0・負数・boolも届き得る。POSIXでは0/負数のpidはプロセス
        # グループ宛のシグナルという別の意味を持ち、生存確認には使えない。
        for bad_pid in (0, -1, True):
            ctx = _ctx(
                tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
                run_state=RunState(
                    active_worktrees={
                        "1": _active(
                            1, pid=bad_pid, external_id=None, launch_phase=None
                        )
                    }
                ),
            )
            assert ctx.launch_fact(1) is None, bad_pid

    def test_launch_fact_is_none_when_external_id_is_not_a_string(self):
        # #868レビュー対応: `run_state.json`が保持し得る非文字列の真値
        # (例: `true`)は、`bool(...)`だけではプロバイダIDとして誤認する。
        for bad_external_id in (True, 12345):
            ctx = _ctx(
                tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
                run_state=RunState(
                    active_worktrees={
                        "1": _active(
                            1,
                            pid=None,
                            external_id=bad_external_id,
                            launch_phase=None,
                        )
                    }
                ),
            )
            assert ctx.launch_fact(1) is None, bad_external_id

    def test_launch_fact_is_none_when_branch_or_worktree_path_is_not_a_string(self):
        # セルフチェックで発見(#868): external_id/pidと同じく、branchと
        # worktree_pathも`run_state.json`から検証されずに復元されるため、
        # 非文字列の真値を`bool(...)`で受け入れるとブランチ名・パスとして
        # 使えない値が確定的なLaunchFactへ載ってしまう。
        for field, bad_value in (
            ("branch", 123),
            ("branch", True),
            ("worktree_path", 999),
        ):
            ctx = _ctx(
                tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
                run_state=RunState(
                    active_worktrees={"1": _active(1, **{field: bad_value})}
                ),
            )
            assert ctx.launch_fact(1) is None, (field, bad_value)

    def test_launch_fact_sanitizes_the_unusable_half_of_a_mixed_handle(self):
        # #868レビュー対応: handle判定は「pidかexternal_idのいずれか」を見る
        # OR判定なので、片方が有効なら不正なもう片方も一緒に通る。型付きの
        # LaunchFactへ載せる前に個別に健全化し、消費側が`external_id is not
        # None`だけを見てプロバイダAPIへbooleanを送る等を防ぐ。
        valid_pid = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={"1": _active(1, pid=111, external_id=True)}
            ),
        )
        fact = valid_pid.launch_fact(1)
        assert fact.pid == 111
        assert fact.external_id is None

        valid_external_id = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={
                    "1": _active(
                        1,
                        pid=-1,
                        external_id="ext-1",
                        started_at="not-a-number",
                        launch_attempt_id=42,
                    )
                }
            ),
        )
        fact = valid_external_id.launch_fact(1)
        assert fact.external_id == "ext-1"
        assert fact.pid is None
        assert fact.started_at is None
        assert fact.launch_attempt_id is None

    @pytest.mark.parametrize("oversized_time", [10**1000, -(10**1000)])
    def test_initial_active_worktree_with_oversized_integer_time_normalizes_to_none(
        self, oversized_time
    ):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={"1": _active(1, pid=42, started_at=oversized_time)}
            ),
        )
        fact = ctx.launch_fact(1)
        assert fact is not None
        assert fact.started_at is None


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
