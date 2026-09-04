"""#796: 依存元(depends_on)のPR・ブランチを外部ロックの衝突検知から除外する。

`tests/test_dispatch_locks.py`の肥大化解消のため、依存元除外に関するテストを
分割している（`test_dispatch_cycle.py`→`test_dispatch_cycle_locks.py`分割と
同じ方針）。
"""

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.locks import scan_external_locks
from orchestune.dispatch.scoring import Task
from orchestune.models import PrRecord


def _task(
    issue_number,
    priority="medium",
    risk=False,
    progress_partial=False,
    created_at="2023-01-01T00:00:00+00:00",
    footprint=("src/foo.py",),
    depends_on=(),
    status_labels=("status:queued",),
    subtask_id=None,
):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id or f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=status_labels,
        created_at=created_at,
        depends_on=depends_on,
    )


class TestDependencyExclusion:
    """#796: `depends_on`で指した依存元のPR・ブランチは、スタッキング起動
    （`orchestune/dispatch/launch.py`の`_get_stack_eligible_tasks`）で
    base取り込みが前提の変更であり、「Orchestune管理外の衝突」ではない。
    依存元由来の重複だけを外部ロックの対象から除外する。"""

    def _dependency_task(self, issue_number=100, subtask_id="dep-a", **kwargs):
        # 依存元タスク自身のfootprintは空にする: 空footprintのタスクは
        # `_collect_task_conflicts`が即座に`()`を返すため、依存元タスク自身が
        # 自分の未アクティブブランチと衝突して(無関係に)ロックされる、この
        # テストの本題ではない挙動を混入させない。
        kwargs.setdefault("footprint", ())
        return _task(issue_number, subtask_id=subtask_id, **kwargs)

    def test_does_not_lock_task_against_dependency_pr(self):
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a"),
                changed_files=("src/shared.py",),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert result.conflicts == {}

    def test_still_locks_task_against_pr_that_merely_mentions_dependency_issue(self):
        """Codexレビュー対応(PR#797): `pr_matches_issue`はPRのタイトル・本文
        中の`#N`言及だけでも一致するため、依存元Issue番号に言及しているだけの
        無関係なPR（headは依存元の正規ブランチと無関係）まで除外してしまうと、
        本物の外部衝突を見逃す。除外は依存元の正規ブランチ名との一致に限る。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/unrelated-refactor",
                changed_files=("src/shared.py",),
                closes_issue_numbers=(100,),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_fork_pr_impersonating_dependency_branch(self):
        """Codexレビュー対応(PR#797): headブランチ名が依存元の正規ブランチと
        文字列上一致しても、フォーク（`is_cross_repository=True`）由来なら
        依存元の実際のスタッキング対象ではないため除外しない。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a"),
                changed_files=("src/shared.py",),
                is_cross_repository=True,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_dependency_branch_pr_with_unknown_origin(self):
        """`is_cross_repository`が不明(`None`)な場合はfail closedで除外しない。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a"),
                changed_files=("src/shared.py",),
                is_cross_repository=None,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_unrelated_pr_despite_dependency(self):
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/other",
                changed_files=("src/shared.py",),
                closes_issue_numbers=(999,),
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_does_not_lock_task_against_dependency_branch(self):
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        branch_name = build_task_branch_name(100, "dep-a")
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[(branch_name, ("src/shared.py",))],
            prs=[],
            active_branches=[],
        )
        assert result.to_lock == []
        assert result.conflicts == {}

    def test_still_locks_task_against_non_default_prefix_branch_matching_dependency_shape(
        self,
    ):
        """Codexレビュー対応(PR#797 P2): 見た目上は依存元の
        `issue-{N}-{subtask_id}`形状に一致するブランチでも、既定prefix
        （`build_task_branch_name`/`subtask_branch_map`が実際に使うブランチ）
        と完全一致しなければスタッキングの取り込み対象ではないため、
        引き続き外部衝突として扱う。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        branch_name = build_task_branch_name(100, "dep-a", prefix="fix")
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[(branch_name, ("src/shared.py",))],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_pr_on_non_default_prefix_dependency_branch(self):
        """Codexレビュー対応(PR#797 P2): PR側でも同様に、既定prefixのブランチ
        と完全一致しない限り除外しない。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a", prefix="fix"),
                changed_files=("src/shared.py",),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_unrelated_branch_despite_dependency(self):
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[("fix/issue-999-other", ("src/shared.py",))],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_dependency_branch_diff_unknown_is_excluded_from_fail_closed(self):
        """#245のfail closedは、依存元自身の差分取得不能では発動しない。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        branch_name = build_task_branch_name(100, "dep-a")
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[(branch_name, None)],
            prs=[],
            active_branches=[],
        )
        assert result.to_lock == []
        assert result.conflicts == {}

    def test_fail_closed_still_applies_for_unrelated_unknown_branch_despite_dependency(
        self,
    ):
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-a",))
        dep_branch_name = build_task_branch_name(100, "dep-a")
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[(dep_branch_name, None), ("feat/unrelated-x", None)],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_dependency_pr_files_truncated_is_excluded(self):
        """truncated状態のPRはfootprintの重なりに関わらず無条件でfail closed
        するため、依存元由来である場合は除外されないと#796の意図を満たさない。"""
        dep_task = self._dependency_task()
        task = _task(1, footprint=("src/unrelated_to_pr.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a"),
                changed_files=("something_else.py",),
                is_cross_repository=False,
                is_files_truncated=True,
            )
        ]
        result = scan_external_locks(
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert result.conflicts == {}

    def test_transitive_dependency_is_not_excluded(self):
        """除外は直接の`depends_on`に限る。祖先依存(推移依存)のPRとの重複は
        引き続き外部ロック対象。"""
        grandparent = self._dependency_task(issue_number=200, subtask_id="dep-c")
        parent = self._dependency_task(
            issue_number=100, subtask_id="dep-b", depends_on=("dep-c",)
        )
        task = _task(1, footprint=("src/shared.py",), depends_on=("dep-b",))
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/dep-c-impl",
                changed_files=("src/shared.py",),
                closes_issue_numbers=(200,),
            )
        ]
        result = scan_external_locks(
            [grandparent, parent, task],
            remote_branches=[],
            prs=prs,
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_unresolvable_dependency_subtask_id_still_locks(self):
        """`depends_on`が指すsubtask_idが候補集合に存在しない（解決不能）場合は
        従来通りfail closedでロック対象のままとする。"""
        task = _task(1, footprint=("src/shared.py",), depends_on=("missing-dep",))
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/other",
                changed_files=("src/shared.py",),
                closes_issue_numbers=(55,),
            )
        ]
        result = scan_external_locks(
            [task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_unlocks_previously_locked_task_when_only_conflict_is_dependency(self):
        """#698の実例: 依存元PRだけがロック理由だったタスクは、依存元除外の
        導入により次サイクルで解除される。"""
        dep_task = self._dependency_task()
        locked_task = _task(
            1,
            footprint=("src/shared.py",),
            depends_on=("dep-a",),
            status_labels=("status:external-lock",),
        )
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(100, "dep-a"),
                changed_files=("src/shared.py",),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [dep_task, locked_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert [t.issue_number for t in result.to_unlock] == [1]
