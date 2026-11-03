"""ActiveWorktree restoration candidate projection."""

from __future__ import annotations

from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.recovery.service import _restoration_candidates
from orchestune.ledger.run_state import RunState
from orchestune.models import IssueRecord, PrRecord
from tests.dispatch_recovery_test_support import _issue_with_footprint


def _project_restoration_candidates(
    _run_state: RunState,
    issues: list[IssueRecord],
    config: DispatcherConfig,
):
    """Project the adapter's authoritative Forge snapshot for focused tests."""
    return list(
        _restoration_candidates(
            issues,
            config.resolved_forge.list_open_prs(),
            config,
        )
    )


class TestRestorationCandidateProjection:
    """Supervisor adapter が観測する復元候補の projection を検証する。"""

    def test_missing_issue_without_pr_decides_synthetic_branch(self, tmp_path):
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "101"
        assert subtask_id == "task-a"
        assert active.core.branch == "claude/issue-101-task-a"
        assert active.core.declared_footprint == ("src/foo.py",)
        assert active.claim.reservation_kind == "footprint"
        # decide層はrun_stateを変更しない
        assert run_state.active_worktrees == {}

    def test_falls_back_to_repository_reservation_on_footprint_error(self, tmp_path):
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["../outside.py"]
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        assert len(result) == 1
        _, _, active = result[0]
        assert active.core.declared_footprint == ()
        assert active.claim.reservation_kind == "repository"

    def test_restores_recompute_count_and_forced_serial_from_issue_body(self, tmp_path):
        """#513再現テスト: run_state.json消失時、Issue本文に永続化された
        recompute_count/forced_serialから復元されるべきだが、現状は常に
        (0, False)へ初期化される（DAG再計算のリトライ上限とforced_serial
        フォールバックが、ステートレスCIランナーでは機能しない）。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=2,
            forced_serial=True,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        active = result[0][2]
        assert active.launch.recompute_count == 2
        assert active.launch.forced_serial is True

    def test_restores_zero_and_false_when_issue_predates_the_fields(self, tmp_path):
        """#513: 本フィールド導入前に作られたIssue（フェンスにフィールドが
        無い）は、後方互換として(0, False)にフォールバックする。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        active = result[0][2]
        assert active.launch.recompute_count == 0
        assert active.launch.forced_serial is False

    def test_treats_legacy_force_serial_label_as_authoritative(self, tmp_path):
        """#516再2巡目レビュー指摘: 本フィールド導入前からforced_serialだった
        Issue（本文にフィールドは無いがstatus:force-serialラベルは付いている）
        を、run_state.json消失後に「直列化されていない」と誤って復元しては
        ならない。ラベルは表示専用だが、本文が沈黙している場合の権威として
        扱う。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        issue = IssueRecord(
            number=issue.number,
            title=issue.title,
            body=issue.body,
            labels=issue.labels + ("status:force-serial",),
            created_at=issue.created_at,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        assert result[0][2].launch.forced_serial is True

    def test_restored_old_issue_has_unknown_start_time(self, tmp_path):
        """#198: Issue作成日時はdispatch開始日時ではないため、復元Taskの
        timeout基準に使ってはならない。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            created_at="2020-01-01T00:00:00+00:00",
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(run_state, [issue], config)

        assert result[0][2].launch.started_at is None

    def test_restored_cloud_task_has_unknown_start_time(self, tmp_path):
        """PRに紐付くクラウドTaskでも、PRメタデータから実行開始時刻は復元できない。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            created_at="2020-01-01T00:00:00+00:00",
        )
        pr = PrRecord(
            number=42,
            head_ref="agent/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.branch_exists",
                return_value=False,
            ),
        ):
            result = _project_restoration_candidates(run_state, [issue], config)

        active = result[0][2]
        assert active.launch.external_id == "42"
        assert active.launch.started_at is None

    def test_missing_issue_base_branch_uses_own_parent_not_multiple_parent_config(
        self,
        tmp_path,
    ):
        """#182: 自己修復はリポジトリ全体のin-progress Issueを対象にするため、
        `config.parent_issue_number`（現在のdispatcher起動時の親）ではなく、
        各Issue自身の`parent`から`base_branch`を復元しなければならない。"""
        run_state = RunState(active_worktrees={})
        issue_under_parent_100 = _issue_with_footprint(
            101, subtask_id="task-a", parent={"number": 100}
        )
        issue_under_parent_200 = _issue_with_footprint(
            201, subtask_id="task-b", parent={"number": 200}
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(
                run_state,
                [issue_under_parent_100, issue_under_parent_200],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["101"].core.base_branch == "parent/issue-100"
        assert active_by_key["201"].core.base_branch == "parent/issue-200"

    def test_yaml_dependency_restores_open_dependency_pr_as_base_branch(self, tmp_path):
        """#305: GitHub MCP起票でnative blocked_byが空でも、Footprint YAMLの
        depends_onから依存PRのhead branchを復元する。

        #886: 本文`depends_on`は自タスクの親IssueでスコープしたEPICスコープの
        共通resolverで解決するため、`parent`が必要（cross-EPIC衝突を防ぐ
        設計上の制約）。
        """
        run_state = RunState(active_worktrees={})
        dependency = _issue_with_footprint(
            101, subtask_id="task-a", parent={"number": 100}
        )
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["task-a"],
            parent={"number": 100},
        )
        dependency_pr = PrRecord(
            number=41,
            head_ref="claude/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[dependency_pr],
        ):
            result = _project_restoration_candidates(
                run_state,
                [dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].core.base_branch == "claude/issue-101-task-a"

    def test_native_blocked_by_takes_precedence_over_yaml_dependency(self, tmp_path):
        run_state = RunState(active_worktrees={})
        yaml_dependency = _issue_with_footprint(101, subtask_id="task-a")
        native_dependency = _issue_with_footprint(103, subtask_id="task-c")
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["task-a"],
            blocked_by=(103,),
        )
        yaml_dependency_pr = PrRecord(
            number=41,
            head_ref="claude/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
            is_cross_repository=False,
        )
        native_dependency_pr = PrRecord(
            number=43,
            head_ref="claude/issue-103-task-c",
            changed_files=(),
            closes_issue_numbers=(103,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[yaml_dependency_pr, native_dependency_pr],
        ):
            result = _project_restoration_candidates(
                run_state,
                [yaml_dependency, native_dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].core.base_branch == "claude/issue-103-task-c"

    def test_unresolved_yaml_dependency_keeps_parent_base_branch(self, tmp_path):
        run_state = RunState(active_worktrees={})
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["unknown-task"],
            parent={"number": 100},
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _project_restoration_candidates(
                run_state,
                [dependent],
                config,
            )

        assert result[0][2].core.base_branch == "parent/issue-100"

    def test_cross_epic_same_subtask_id_does_not_collide(self, tmp_path):
        """#886: 2つの異なるEPIC（親Issue）配下に、同名subtask_idを持つ
        別々のタスクが存在する母集団でも、depends_onは自タスクの親でスコープ
        して解決しなければならない（旧: グローバルな
        `{subtask_id: issue_number}`辞書は最後に見つかった方で上書きされ、
        別EPICのタスクを誤って依存先に選んでしまうバグがあった）。
        """
        run_state = RunState(active_worktrees={})
        # 両方のEPICに"task-a"という名前のタスクが存在する。
        epic_a_dependency = _issue_with_footprint(
            201, subtask_id="task-a", parent={"number": 100}
        )
        epic_b_dependency = _issue_with_footprint(
            301, subtask_id="task-a", parent={"number": 200}
        )
        epic_b_dependent = _issue_with_footprint(
            302,
            subtask_id="task-b",
            depends_on=["task-a"],
            parent={"number": 200},
        )
        epic_a_pr = PrRecord(
            number=41,
            head_ref="claude/issue-201-task-a",
            changed_files=(),
            closes_issue_numbers=(201,),
            is_cross_repository=False,
        )
        epic_b_pr = PrRecord(
            number=42,
            head_ref="claude/issue-301-task-a",
            changed_files=(),
            closes_issue_numbers=(301,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[epic_a_pr, epic_b_pr],
        ):
            result = _project_restoration_candidates(
                run_state,
                [epic_a_dependency, epic_b_dependency, epic_b_dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        # epic_b_dependent (parent=200) must resolve "task-a" to epic_b's own
        # #301, never epic_a's #201, regardless of open_prs iteration order.
        assert active_by_key["302"].core.base_branch == "claude/issue-301-task-a"

    def test_partially_unresolved_dependencies_still_use_the_resolved_ones(
        self, tmp_path
    ):
        """#886: 複数の本文依存のうち一部が未解決でも、解決できた依存の
        PRは採用してよい（「一部未解決」は「全て未採用」を意味しない）。
        """
        run_state = RunState(active_worktrees={})
        resolvable = _issue_with_footprint(
            101, subtask_id="task-a", parent={"number": 100}
        )
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["task-a", "unknown-task"],
            parent={"number": 100},
        )
        resolvable_pr = PrRecord(
            number=41,
            head_ref="claude/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[resolvable_pr],
        ):
            result = _project_restoration_candidates(
                run_state,
                [resolvable, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].core.base_branch == "claude/issue-101-task-a"

    def test_native_blocked_by_without_subtask_identity_fails_closed(self, tmp_path):
        """#783: blockerのsubtask identityが母集団から取得できない場合、
        Issue番号だけでPRを採用せず既定baseへfail closedする。
        """
        run_state = RunState(active_worktrees={})
        # blocker issue 103 is intentionally NOT included in `issues` below --
        # it has already left the in-progress/queued population (e.g. done).
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            blocked_by=(103,),
        )
        blocker_pr = PrRecord(
            number=43,
            head_ref="claude/issue-103-task-c",
            changed_files=(),
            closes_issue_numbers=(103,),
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[blocker_pr],
        ):
            result = _project_restoration_candidates(
                run_state,
                [dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].core.base_branch == "origin/main"

    def test_restores_active_worktree_from_open_pr_with_empty_closes_issues_via_head_ref(
        self, tmp_path
    ):
        """#739: 非デフォルトブランチ宛てPRでcloses_issue_numbersが空でも、
        head_ref（例: codex/issue-709-guarded-repair-rollout）からPR番号とブランチを復元する。
        """
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            709, subtask_id="guarded-repair-rollout", footprint=["src/foo.py"]
        )
        pr = PrRecord(
            number=737,
            head_ref="codex/issue-709-guarded-repair-rollout",
            changed_files=("src/foo.py",),
            closes_issue_numbers=(),
            base_ref="parent/issue-700",
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.branch_exists",
                return_value=False,
            ),
        ):
            result = _project_restoration_candidates(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "709"
        assert subtask_id == "guarded-repair-rollout"
        assert active.core.branch == "codex/issue-709-guarded-repair-rollout"
        assert active.launch.external_id == "737"
        assert active.launch.external_url == "PR#737"

    def test_restores_open_dependency_pr_with_empty_closes_issues_as_base_branch(
        self, tmp_path
    ):
        """#739: 依存先PRのcloses_issue_numbersが空でもhead_refから依存PRを解決する。

        #886: 本文`depends_on`はEPICスコープの共通resolverで解決するため`parent`が必要。
        """
        run_state = RunState(active_worktrees={})
        dependency = _issue_with_footprint(
            709, subtask_id="task-a", parent={"number": 700}
        )
        dependent = _issue_with_footprint(
            710,
            subtask_id="task-b",
            depends_on=["task-a"],
            parent={"number": 700},
        )
        dependency_pr = PrRecord(
            number=737,
            head_ref="codex/issue-709-task-a",
            changed_files=(),
            closes_issue_numbers=(),
            base_ref="parent/issue-700",
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.list_open_prs",
                return_value=[dependency_pr],
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.branch_exists",
                return_value=False,
            ) as mock_branch_exists,
        ):
            result = _project_restoration_candidates(
                run_state,
                [dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["710"].core.base_branch == "codex/issue-709-task-a"
        mock_branch_exists.assert_called_once_with("claude/issue-709-task-a")
