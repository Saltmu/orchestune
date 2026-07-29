"""統合ブランチの公開: `PushTempBranchStep` / `EnsureIntegrationPrStep` /
`SemanticReviewStep`。

push成功を後続の前提条件として扱い（#52）、統合PRを冪等に確保し、
意味的レビューをfire-and-forgetで委譲するところまでを扱う。
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import Mock

from orchestune.dispatch_targets import DispatchHandle
from orchestune.integration_coordinator import IntegrationCoordinator
from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.models import PrRecord
from tests.conftest import IntegratorEnv, make_done_issue


class _RecordingCoordinator(IntegrationCoordinator):
    """`dispatch_review`の呼び出し引数を記録するテストダブル。

    実シグネチャをそのまま実装することで、引数名の変更もテスト側で検知できる。
    """

    def __init__(self, handle: DispatchHandle | None = None):
        self.calls: list[dict] = []
        self._handle = handle or DispatchHandle(external_id="s")

    def dispatch_review(
        self,
        temp_branch: str,
        base_branch: str,
        pr_number: int,
        parent_issue_number: int | None,
        merged_subtask_ids: Sequence[str],
    ) -> DispatchHandle:
        self.calls.append(
            {
                "temp_branch": temp_branch,
                "base_branch": base_branch,
                "pr_number": pr_number,
                "parent_issue_number": parent_issue_number,
                "merged_subtask_ids": list(merged_subtask_ids),
            }
        )
        return self._handle


class TestEnsureIntegrationPr:
    def test_reuses_existing_open_pr(self, integrator_env: IntegratorEnv):
        # 既にintegration/temp-main→mainのopenなPRがある場合は重複作成しない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        # #243: identityを検証できたPR（upstream上の正規head・指定base向け）
        # だけが再利用対象になる。
        integrator_env.list_open_prs.return_value = [
            PrRecord(
                number=777,
                head_ref="integration/temp-main",
                changed_files=(),
                base_ref="main",
                is_cross_repository=False,
            )
        ]

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["integration_pr_number"] == 777
        integrator_env.create_pull_request.assert_not_called()

    def test_creation_failure_is_non_fatal_but_blocks_inclusion_label(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.side_effect = RuntimeError(
            "no commits between main and branch"
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert res["integration_pr_number"] is None
        assert res["semantic_review_dispatched"] is False
        # #139: PR確保に失敗した場合、統合の安全確定ができていないため
        # integration:includedは付与しない。
        assert res["newly_included"] == []
        integrator_env.add_label.assert_not_called()


class TestPushTempBranchFailure:
    """#52: 統合ブランチのpush失敗後もPR作成・レビューを続行し、成功扱いになっていた
    不具合の回帰テスト。"""

    def test_without_existing_pr_returns_explicit_failure(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(lambda args: "push" in args, stderr="remote rejected")

        res = Integrator(IntegratorConfig(apply=True)).run()

        # push失敗をPR作成の前提条件にし、成功扱いにしない。
        assert res["status"] == "failed_to_push_temp_branch"
        assert res["merged"] == ["task-1"]
        assert "remote rejected" in res["error"]
        integrator_env.list_open_prs.assert_not_called()
        integrator_env.create_pull_request.assert_not_called()
        # #139: push失敗時は統合の安全確定ができていないため、
        # integration:includedを付与してはならない。
        integrator_env.add_label.assert_not_called()

    def test_with_existing_pr_does_not_reuse_or_review(
        self, integrator_env: IntegratorEnv
    ):
        # push失敗時に既存の統合PRがある場合でも、リモート上の古いdiffに対して
        # semantic reviewを起動したり、それを再利用してsuccessを返してはならない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(lambda args: "push" in args, stderr="non-fast-forward")
        integrator_env.list_open_prs.return_value = [
            PrRecord(number=555, head_ref="integration/temp-main", changed_files=())
        ]

        coordinator = Mock(spec=["dispatch_review"])
        res = Integrator(IntegratorConfig(apply=True, coordinator=coordinator)).run()

        assert res["status"] == "failed_to_push_temp_branch"
        assert "integration_pr_number" not in res
        assert "semantic_review_dispatched" not in res
        integrator_env.list_open_prs.assert_not_called()
        integrator_env.create_pull_request.assert_not_called()
        coordinator.dispatch_review.assert_not_called()

    def test_non_ascii_stderr_is_surfaced_as_text(self, integrator_env: IntegratorEnv):
        # run_gitはtext=Trueで実行されるため、非ASCII文字を含むエラー出力も
        # そのまま文字列として安全に扱えることを確認する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "push" in args, stderr="拒否されました: non-fast-forward"
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failed_to_push_temp_branch"
        assert isinstance(res["error"], str)
        assert "拒否されました" in res["error"]


class TestSemanticReview:
    def test_dispatched_fire_and_forget_after_pr_created(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.return_value = 315
        coordinator = _RecordingCoordinator(
            DispatchHandle(external_id="s1", external_url="https://claude.ai/code/s/s1")
        )

        config = IntegratorConfig(
            apply=True,
            parent_issue_number=100,
            enable_semantic_review=True,
            coordinator=coordinator,
        )
        res = Integrator(config).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert res["integration_pr_number"] == 315
        assert res["semantic_review_dispatched"] is True

        # レビューは統合PR番号付きでfire-and-forgetで起動される。結果を待ったり、
        # 状態を記録したりはしない（自動マージ等の後続処理が無くなったため）。
        assert len(coordinator.calls) == 1
        assert coordinator.calls[0]["merged_subtask_ids"] == ["task-1"]
        assert (
            coordinator.calls[0]["temp_branch"] == "integration/temp-parent-issue-100"
        )
        assert coordinator.calls[0]["pr_number"] == 315

        # ブランチのforce pushは行われる（起動セッションがレビューできるように）
        assert len(integrator_env.calls_with("push")) == 1

    def test_explicitly_disabled_is_not_dispatched(self, integrator_env: IntegratorEnv):
        # enable_semantic_review=False を明示するとレビューは委譲されない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.return_value = 315
        coordinator = _RecordingCoordinator()

        config = IntegratorConfig(
            apply=True,
            parent_issue_number=100,
            enable_semantic_review=False,
            coordinator=coordinator,
        )
        res = Integrator(config).run()

        assert res["status"] == "success"
        assert coordinator.calls == []
        assert res["semantic_review_dispatched"] is False

    def test_default_on_but_skipped_without_coordinator(
        self, integrator_env: IntegratorEnv
    ):
        # 既定ONだが coordinator 未注入なら安全にスキップ（既存の直接構築呼び出しを壊さない）。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.return_value = 315

        config = IntegratorConfig(apply=True)  # coordinator=None, enable=既定True
        assert config.enable_semantic_review is True
        res = Integrator(config).run()

        assert res["status"] == "success"
        assert res["semantic_review_dispatched"] is False
