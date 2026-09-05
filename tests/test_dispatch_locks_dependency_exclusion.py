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
    parent_number=100,
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
        # #799: 本ファイルのタスク群は同一EPIC内の依存解決を検証するため、
        # 共通のparent_numberでスコープする。
        parent_number=parent_number,
    )


class TestDependencyExclusion:
    """#796: `depends_on`で指した依存元のPR・ブランチは、スタッキング起動
    （`orchestune/dispatch/launch.py`の`_get_stack_eligible_tasks`）で
    base取り込みが前提の変更であり、「Orchestune管理外の衝突」ではない。
    依存元由来の重複だけを外部ロックの対象から除外する。

    スタッキングは`status:blocked`のタスクにしか起こらない
    （`_get_stack_eligible_tasks`は`issues.blocked`のみを走査する）ため、
    除外を検証するテストの依存タスク側は`status:blocked`にする
    （Codexレビュー対応 PR#797 P2, Finding 5）。"""

    def _dependency_task(self, issue_number=100, subtask_id="dep-a", **kwargs):
        # 依存元タスク自身のfootprintは空にする: 空footprintのタスクは
        # `_collect_task_conflicts`が即座に`()`を返すため、依存元タスク自身が
        # 自分の未アクティブブランチと衝突して(無関係に)ロックされる、この
        # テストの本題ではない挙動を混入させない。
        kwargs.setdefault("footprint", ())
        return _task(issue_number, subtask_id=subtask_id, **kwargs)

    def _blocked_task(self, issue_number=1, **kwargs):
        kwargs.setdefault("status_labels", ("status:blocked",))
        return _task(issue_number, **kwargs)

    def test_does_not_lock_task_against_dependency_pr(self):
        dep_task = self._dependency_task()
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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

    def test_still_locks_queued_task_with_unresolved_dependency(self):
        """Codexレビュー対応(PR#797 P2, Finding 5): `status:queued`のまま
        依存が未解決という異常系（`QUEUED_WITH_UNRESOLVED_DEPENDENCIES`が
        検知・修復する状態）では、スタッキング（`status:blocked`前提）が
        起こらないため除外しない。`_filter_queued_candidates`はfootprintや
        `depends_on`を見ずに候補として扱うため、除外すると依存元の変更が
        実際には入っていないbaseから起動されてしまう。"""
        dep_task = self._dependency_task()
        task = _task(
            1,
            footprint=("src/shared.py",),
            depends_on=("dep-a",),
            status_labels=("status:queued",),
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
            [dep_task, task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_done_dependency_pr_not_yet_merged(self):
        """Codexレビュー対応(PR#797 P2, Finding 4): 依存元が`status:done`に
        到達すると`_is_task_stack_eligible`はそれをstackable_depsに加えない
        （＝スタックのbaseとして選ばれない）ため、このタスクは通常の
        parent/mainからそのまま起動され得る。Integratorのマージが未完了で
        依存元PRの変更が実際にはbaseへ入っていない場合、これは正真正銘の
        外部衝突であり除外してはならない。"""
        dep_task = self._dependency_task(status_labels=("status:done",))
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_done_dependency_branch_not_yet_merged(self):
        dep_task = self._dependency_task(status_labels=("status:done",))
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
        branch_name = build_task_branch_name(100, "dep-a")
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[(branch_name, ("src/shared.py",))],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_not_needed_dependency_pr(self):
        dep_task = self._dependency_task(status_labels=("status:not-needed",))
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_still_locks_task_against_pr_that_merely_mentions_dependency_issue(self):
        """Codexレビュー対応(PR#797): `pr_matches_issue`はPRのタイトル・本文
        中の`#N`言及だけでも一致するため、依存元Issue番号に言及しているだけの
        無関係なPR（headは依存元の正規ブランチと無関係）まで除外してしまうと、
        本物の外部衝突を見逃す。除外は依存元の正規ブランチ名との一致に限る。"""
        dep_task = self._dependency_task()
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
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
        task = self._blocked_task(
            footprint=("src/unrelated_to_pr.py",), depends_on=("dep-a",)
        )
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
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-b",))
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
        task = self._blocked_task(
            footprint=("src/shared.py",), depends_on=("missing-dep",)
        )
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
        locked_task = self._blocked_task(
            footprint=("src/shared.py",),
            depends_on=("dep-a",),
            status_labels=("status:blocked", "status:external-lock"),
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

    def test_same_subtask_id_in_different_epic_is_not_treated_as_dependency(self):
        """#799: 別EPIC（別parent）に同名subtask_idの依存元らしきタスクが
        あっても、依存元として同定して外部ロックから除外してはならない。"""
        other_epic_dep = self._dependency_task(
            issue_number=900, subtask_id="dep-a", parent_number=200
        )
        task = self._blocked_task(footprint=("src/shared.py",), depends_on=("dep-a",))
        prs = [
            PrRecord(
                number=99,
                head_ref=build_task_branch_name(900, "dep-a"),
                changed_files=("src/shared.py",),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [other_epic_dep, task], remote_branches=[], prs=prs, active_branches=[]
        )
        # `dep-a`はtaskと同じparent(100)配下には存在しないため未解決＝fail
        # closedのまま。別EPIC(#900)のPRとの重複を依存元として除外しない。
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_partial_unresolved_dependency_disables_all_exclusions(self):
        """#799レビュー指摘(Codex P2): 依存の一部が未解決（同一親内の重複＝
        曖昧）な場合、他の依存が解決済みでも一切除外してはならない。
        `_is_task_stack_eligible`は未解決が1件でもあればスタック不可と
        判定するため、このタスクは通常のparent/mainから起動され得る——
        その場合、解決済みだった依存元ブランチとの重複も正真正銘の外部衝突。
        """
        dep_a = self._dependency_task(issue_number=100, subtask_id="dep-a")
        ambiguous_dep_b1 = self._dependency_task(issue_number=101, subtask_id="dep-b")
        ambiguous_dep_b2 = self._dependency_task(issue_number=102, subtask_id="dep-b")
        task = self._blocked_task(
            footprint=("src/shared.py",), depends_on=("dep-a", "dep-b")
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
            [dep_a, ambiguous_dep_b1, ambiguous_dep_b2, task],
            remote_branches=[],
            prs=prs,
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]
