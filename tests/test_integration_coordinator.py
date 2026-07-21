from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.integration_coordinator import (
    NOT_NEEDED_REJECTED_LABEL,
    NOT_NEEDED_VERIFIED_LABEL,
    IntegrationCoordinator,
    build_integration_coordinator,
    build_not_needed_review_prompt,
    build_review_routine_prompt,
    process_pending_not_needed_reviews,
    record_pending_not_needed_review,
)
from orchestune.not_needed_review_state import (
    NotNeededReviewState,
    PendingNotNeededReview,
    load_not_needed_review_state,
    save_not_needed_review_state,
)


class _FakeFirer:
    def __init__(self, handle: DispatchHandle):
        self._handle = handle
        self.fired: list[str] = []

    def fire_text(self, text: str) -> DispatchHandle:
        self.fired.append(text)
        return self._handle


class TestBuildReviewRoutinePrompt:
    def test_contains_branch_pr_subtasks_and_parent(self):
        prompt = build_review_routine_prompt(
            temp_branch="integration/temp-main",
            base_branch="origin/main",
            pr_number=315,
            parent_issue_number=181,
            merged_subtask_ids=["task-1", "task-2"],
        )
        assert "integration/temp-main" in prompt
        assert "origin/main" in prompt
        assert "task-1, task-2" in prompt
        assert "#181" in prompt
        assert "#315" in prompt

    def test_instructs_pr_comment_and_forbids_merge_for_parent_issue(self):
        prompt = build_review_routine_prompt(
            temp_branch="integration/temp-main",
            base_branch="origin/main",
            pr_number=315,
            parent_issue_number=181,
            merged_subtask_ids=["task-1"],
        )
        assert "gh pr comment 315" in prompt
        assert "gh pr merge" in prompt
        assert "絶対に実行しないでください" in prompt
        assert (
            "本PRは統合システムのパイプラインによって自動マージ・管理されます" in prompt
        )

    def test_instructs_pr_comment_and_forbids_merge_for_main_integration(self):
        prompt = build_review_routine_prompt(
            temp_branch="integration/temp-main",
            base_branch="origin/main",
            pr_number=315,
            parent_issue_number=None,
            merged_subtask_ids=["task-1"],
        )
        assert "gh pr comment 315" in prompt
        assert "gh pr merge" in prompt
        assert "絶対に実行しないでください" in prompt
        assert "最終的なマージ判断は人間が行います" in prompt

    def test_does_not_carry_prior_findings(self):
        # 再レビュー時のバイアス回避: プロンプトは過去の指摘を含めない設計。
        prompt = build_review_routine_prompt(
            temp_branch="integration/temp-main",
            base_branch="origin/main",
            pr_number=315,
            parent_issue_number=None,
            merged_subtask_ids=["task-1"],
        )
        assert "前回のレビュー内容は与えられていません" in prompt


class TestIntegrationCoordinatorDispatchReview:
    def test_fires_routine_with_prompt_and_returns_handle(self):
        handle = DispatchHandle(
            external_id="sess-1", external_url="https://claude.ai/code/s/sess-1"
        )
        firer = _FakeFirer(handle)
        coord = IntegrationCoordinator(firer)

        result = coord.dispatch_review(
            temp_branch="integration/temp-main",
            base_branch="origin/main",
            pr_number=315,
            parent_issue_number=181,
            merged_subtask_ids=["task-1", "task-2"],
        )

        assert result is handle
        assert len(firer.fired) == 1
        assert "task-1, task-2" in firer.fired[0]
        assert "integration/temp-main" in firer.fired[0]

    def test_each_dispatch_fires_a_fresh_routine_session(self):
        # 差し戻し後の再レビューで新規セッションを使う保証:
        # dispatch_review 呼び出しごとに fire_text が呼ばれる（=新規セッション起動）。
        firer = _FakeFirer(DispatchHandle(external_id="s"))
        coord = IntegrationCoordinator(firer)
        coord.dispatch_review(
            "integration/temp-main", "origin/main", 315, 1, ["task-1"]
        )
        coord.dispatch_review(
            "integration/temp-main", "origin/main", 315, 1, ["task-1"]
        )
        assert len(firer.fired) == 2


class TestBuildIntegrationCoordinator:
    def test_none_without_routine_credentials(self, monkeypatch):
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        assert build_integration_coordinator() is None

    def test_none_when_only_one_credential_present(self, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_ID", "rid")
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        assert build_integration_coordinator() is None

    def test_builds_with_cloud_routine_target_when_credentials_present(
        self, monkeypatch
    ):
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_ID", "rid")
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_TOKEN", "rtok")
        coord = build_integration_coordinator()
        assert isinstance(coord, IntegrationCoordinator)
        assert isinstance(coord._routine_firer, ClaudeCodeCloudRoutineDispatchTarget)


class TestBuildNotNeededReviewPrompt:
    """#282: status:not-needed判定を独立検証させるプロンプト。"""

    def test_contains_issue_number_and_subtask(self):
        prompt = build_not_needed_review_prompt(250, "plot-api-routes")
        assert "#250" in prompt
        assert "plot-api-routes" in prompt

    def test_instructs_verified_label_without_closing(self):
        prompt = build_not_needed_review_prompt(250, "plot-api-routes")
        assert NOT_NEEDED_VERIFIED_LABEL in prompt
        # Issueクローズ自体はPython側の責務であり、レビューセッションは
        # 明示的に禁止されている（既存の意味的レビューがgh pr mergeを禁じるのと同型）。
        assert "gh issue close" in prompt
        assert "絶対に実行しないでください" in prompt

    def test_instructs_rejection_requeues_and_comments(self):
        prompt = build_not_needed_review_prompt(250, "plot-api-routes")
        assert NOT_NEEDED_REJECTED_LABEL in prompt
        assert "status:queued" in prompt
        assert "status:not-needed" in prompt

    def test_does_not_carry_prior_findings(self):
        prompt = build_not_needed_review_prompt(250, "plot-api-routes")
        assert "前回のレビュー内容は与えられていません" in prompt


class TestIntegrationCoordinatorDispatchNotNeededReview:
    def test_fires_routine_with_prompt_and_returns_handle(self):
        handle = DispatchHandle(
            external_id="sess-1", external_url="https://claude.ai/code/s/sess-1"
        )
        firer = _FakeFirer(handle)
        coord = IntegrationCoordinator(firer)

        result = coord.dispatch_not_needed_review(250, "plot-api-routes")

        assert result is handle
        assert len(firer.fired) == 1
        assert "#250" in firer.fired[0]
        assert "plot-api-routes" in firer.fired[0]


class TestRecordPendingNotNeededReview:
    def test_appends_pending_entry_with_session_handle(self, tmp_path):
        path = tmp_path / "state.json"
        handle = DispatchHandle(
            external_id="sess-1", external_url="https://claude.ai/code/s/sess-1"
        )
        record_pending_not_needed_review(
            path,
            issue_number=250,
            subtask_id="plot-api-routes",
            session_handle=handle,
        )

        state = load_not_needed_review_state(path)
        assert len(state.pending) == 1
        entry = state.pending[0]
        assert entry.issue_number == 250
        assert entry.subtask_id == "plot-api-routes"
        assert entry.session_external_id == "sess-1"

    def test_appends_without_clobbering_existing_pending_entries(self, tmp_path):
        path = tmp_path / "state.json"
        record_pending_not_needed_review(
            path, issue_number=1, subtask_id="a", session_handle=DispatchHandle()
        )
        record_pending_not_needed_review(
            path, issue_number=2, subtask_id="b", session_handle=DispatchHandle()
        )
        state = load_not_needed_review_state(path)
        assert len(state.pending) == 2


class TestProcessPendingNotNeededReviews:
    def _state_with(self, *entries: PendingNotNeededReview, path):
        save_not_needed_review_state(NotNeededReviewState(pending=list(entries)), path)

    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_no_pending_reviews_is_a_noop(self, mock_labels, tmp_path):
        path = tmp_path / "state.json"
        result = process_pending_not_needed_reviews(path)
        assert result == {"closed": [], "reopened": [], "still_pending": 0}
        mock_labels.assert_not_called()

    @patch("orchestune.integration_coordinator.github.get_issue_state")
    @patch("orchestune.integration_coordinator.github.close_issue")
    @patch("orchestune.integration_coordinator.github.remove_label")
    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_verified_label_closes_issue_and_mentions_human(
        self, mock_labels, mock_remove, mock_close, mock_state, tmp_path
    ):
        path = tmp_path / "state.json"
        self._state_with(
            PendingNotNeededReview(
                issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
            ),
            path=path,
        )
        mock_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        mock_state.return_value = "OPEN"
        call_order: list[str] = []
        mock_close.side_effect = lambda *a, **kw: call_order.append("close")
        mock_remove.side_effect = lambda *a, **kw: call_order.append("remove")

        result = process_pending_not_needed_reviews(path)

        mock_close.assert_called_once()
        close_args = mock_close.call_args.args
        close_kwargs = mock_close.call_args.kwargs
        assert close_args[0] == 250
        assert close_args[1] == "not planned"
        assert "@Saltmu" in close_kwargs["comment"]
        mock_remove.assert_called_once_with(250, NOT_NEEDED_VERIFIED_LABEL)
        # close_issueがremove_labelより先に呼ばれること（#205: クローズ成功確定前に
        # 完了シグナルを消費しない）。
        assert call_order == ["close", "remove"]
        assert result["closed"] == [250]
        assert result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    @patch("orchestune.integration_coordinator.github.get_issue_state")
    @patch("orchestune.integration_coordinator.github.close_issue")
    @patch("orchestune.integration_coordinator.github.remove_label")
    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_close_failure_keeps_passed_label_unconsumed_and_entry_pending(
        self, mock_labels, mock_remove, mock_close, mock_state, tmp_path
    ):
        """#205: close_issueが失敗した場合、passedラベルが消費されず、エントリは
        pendingのまま残って次サイクルで再試行されること。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        mock_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        mock_state.return_value = "OPEN"
        mock_close.side_effect = RuntimeError("gh api error")

        result = process_pending_not_needed_reviews(path)

        mock_remove.assert_not_called()
        assert result["closed"] == []
        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

    @patch("orchestune.integration_coordinator.github.get_issue_state")
    @patch("orchestune.integration_coordinator.github.close_issue")
    @patch("orchestune.integration_coordinator.github.remove_label")
    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_one_entry_failure_does_not_block_others_from_saving(
        self, mock_labels, mock_remove, mock_close, mock_state, tmp_path
    ):
        """#205: 1エントリの失敗が、他エントリの処理結果の状態保存を巻き込まない
        こと。"""
        path = tmp_path / "state.json"
        ok_entry = PendingNotNeededReview(
            issue_number=100, subtask_id="ok", dispatched_at=1.0
        )
        failing_entry = PendingNotNeededReview(
            issue_number=200, subtask_id="fails", dispatched_at=1.0
        )
        self._state_with(ok_entry, failing_entry, path=path)
        mock_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        mock_state.return_value = "OPEN"
        mock_close.side_effect = lambda issue_number, *a, **kw: (
            (_ for _ in ()).throw(RuntimeError("gh api error"))
            if issue_number == 200
            else None
        )

        result = process_pending_not_needed_reviews(path)

        assert result["closed"] == [100]
        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [failing_entry]

    @patch("orchestune.integration_coordinator.github.get_issue_state")
    @patch("orchestune.integration_coordinator.github.close_issue")
    @patch("orchestune.integration_coordinator.github.remove_label")
    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_remove_label_failure_after_close_retries_without_double_closing(
        self, mock_labels, mock_remove, mock_close, mock_state, tmp_path
    ):
        """#205: クローズ成功後にremove_labelが失敗しても、次サイクルの再試行で
        二重クローズが発生しないこと（冪等）。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        mock_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        mock_state.return_value = "OPEN"
        mock_remove.side_effect = RuntimeError("gh api error")

        first_result = process_pending_not_needed_reviews(path)

        mock_close.assert_called_once()
        assert first_result["closed"] == []
        assert first_result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

        # 次サイクル: 実際にはクローズは成功済みなのでIssueはCLOSED、
        # ラベルはまだ消費できていないのでpassedラベルは残ったまま。
        mock_close.reset_mock()
        mock_remove.reset_mock(side_effect=True)
        mock_state.return_value = "CLOSED"

        second_result = process_pending_not_needed_reviews(path)

        mock_close.assert_not_called()
        mock_remove.assert_called_once_with(250, NOT_NEEDED_VERIFIED_LABEL)
        assert second_result["closed"] == [250]
        assert second_result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    @patch("orchestune.integration_coordinator.github.close_issue")
    @patch("orchestune.integration_coordinator.github.remove_label")
    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_rejected_label_clears_without_closing(
        self, mock_labels, mock_remove, mock_close, tmp_path
    ):
        path = tmp_path / "state.json"
        self._state_with(
            PendingNotNeededReview(
                issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
            ),
            path=path,
        )
        mock_labels.return_value = (NOT_NEEDED_REJECTED_LABEL,)

        result = process_pending_not_needed_reviews(path)

        mock_close.assert_not_called()
        mock_remove.assert_called_once_with(250, NOT_NEEDED_REJECTED_LABEL)
        assert result["reopened"] == [250]
        assert load_not_needed_review_state(path).pending == []

    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_neither_label_present_keeps_entry_pending(self, mock_labels, tmp_path):
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        mock_labels.return_value = ("status:not-needed",)

        result = process_pending_not_needed_reviews(path)

        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_label_polling_failure_keeps_entry_pending(self, mock_labels, tmp_path):
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        mock_labels.side_effect = RuntimeError("gh api error")

        result = process_pending_not_needed_reviews(path)

        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

    @patch("orchestune.integration_coordinator.github.get_issue_labels")
    def test_base_exception_mid_loop_preserves_full_pending_state(
        self, mock_labels, tmp_path
    ):
        """#226: BaseException（割り込み・強制終了）でループが中断した場合、
        状態ファイルを切り詰めず、未処理エントリを含む全pendingエントリを温存する
        こと（次サイクルで全件を再処理できるようにするため）。

        #205修正のtry/finally保存は、通常のExceptionは内側で捕捉済みのため
        BaseException時にしか発火せず、その際に未処理エントリを取りこぼした
        still_pendingを書き込んでいた（同種の恒久リーク）。
        """
        path = tmp_path / "state.json"
        first = PendingNotNeededReview(
            issue_number=100, subtask_id="first", dispatched_at=1.0
        )
        second = PendingNotNeededReview(
            issue_number=200, subtask_id="second", dispatched_at=1.0
        )
        self._state_with(first, second, path=path)

        def labels_side_effect(issue_number):
            if issue_number == 200:
                raise KeyboardInterrupt()
            return ("status:not-needed",)

        mock_labels.side_effect = labels_side_effect

        with pytest.raises(KeyboardInterrupt):
            process_pending_not_needed_reviews(path)

        # 中断時は状態ファイルを書き換えないため、元の全pendingエントリが温存される。
        remaining = {p.issue_number for p in load_not_needed_review_state(path).pending}
        assert remaining == {100, 200}
