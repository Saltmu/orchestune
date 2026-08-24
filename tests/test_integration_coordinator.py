from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.infra.not_needed_review_state import (
    NotNeededReviewState,
    PendingNotNeededReview,
    load_not_needed_review_state,
    save_not_needed_review_state,
)
from orchestune.integrator.coordinator import (
    DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
    NOT_NEEDED_REJECTED_LABEL,
    NOT_NEEDED_VERIFIED_LABEL,
    IntegrationCoordinator,
    build_integration_coordinator,
    build_not_needed_review_prompt,
    build_review_routine_prompt,
    process_pending_not_needed_reviews,
    record_pending_not_needed_review,
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

    def test_no_pending_reviews_is_a_noop(self, fake_forge: MagicMock, tmp_path):
        path = tmp_path / "state.json"
        result = process_pending_not_needed_reviews(path, forge=fake_forge)
        assert result == {
            "closed": [],
            "reopened": [],
            "timed_out": [],
            "still_pending": 0,
        }
        fake_forge.get_issue_labels.assert_not_called()

    def test_verified_label_closes_issue_and_mentions_human(
        self, fake_forge: MagicMock, tmp_path
    ):
        path = tmp_path / "state.json"
        self._state_with(
            PendingNotNeededReview(
                issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
            ),
            path=path,
        )
        fake_forge.get_issue_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        fake_forge.get_issue_state.return_value = "OPEN"
        call_order: list[str] = []
        fake_forge.close_issue.side_effect = lambda *a, **kw: call_order.append("close")
        fake_forge.remove_label.side_effect = lambda *a, **kw: call_order.append(
            "remove"
        )

        result = process_pending_not_needed_reviews(path, forge=fake_forge)

        fake_forge.close_issue.assert_called_once()
        close_args = fake_forge.close_issue.call_args.args
        close_kwargs = fake_forge.close_issue.call_args.kwargs
        assert close_args[0] == 250
        assert close_args[1] == "not planned"
        assert "@Saltmu" in close_kwargs["comment"]
        fake_forge.remove_label.assert_called_once_with(250, NOT_NEEDED_VERIFIED_LABEL)
        # close_issueがremove_labelより先に呼ばれること（#205: クローズ成功確定前に
        # 完了シグナルを消費しない）。
        assert call_order == ["close", "remove"]
        assert result["closed"] == [250]
        assert result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    def test_close_failure_keeps_passed_label_unconsumed_and_entry_pending(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#205: close_issueが失敗した場合、passedラベルが消費されず、エントリは
        pendingのまま残って次サイクルで再試行されること。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.close_issue.side_effect = RuntimeError("gh api error")

        result = process_pending_not_needed_reviews(path, forge=fake_forge)

        fake_forge.remove_label.assert_not_called()
        assert result["closed"] == []
        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

    def test_one_entry_failure_does_not_block_others_from_saving(
        self, fake_forge: MagicMock, tmp_path
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
        fake_forge.get_issue_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.close_issue.side_effect = lambda issue_number, *a, **kw: (
            (_ for _ in ()).throw(RuntimeError("gh api error"))
            if issue_number == 200
            else None
        )

        result = process_pending_not_needed_reviews(path, forge=fake_forge)

        assert result["closed"] == [100]
        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [failing_entry]

    def test_remove_label_failure_after_close_retries_without_double_closing(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#205: クローズ成功後にremove_labelが失敗しても、次サイクルの再試行で
        二重クローズが発生しないこと（冪等）。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = (NOT_NEEDED_VERIFIED_LABEL,)
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.remove_label.side_effect = RuntimeError("gh api error")

        first_result = process_pending_not_needed_reviews(path, forge=fake_forge)

        fake_forge.close_issue.assert_called_once()
        assert first_result["closed"] == []
        assert first_result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

        # 次サイクル: 実際にはクローズは成功済みなのでIssueはCLOSED、
        # ラベルはまだ消費できていないのでpassedラベルは残ったまま。
        fake_forge.close_issue.reset_mock()
        fake_forge.remove_label.reset_mock(side_effect=True)
        fake_forge.get_issue_state.return_value = "CLOSED"

        second_result = process_pending_not_needed_reviews(path, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        fake_forge.remove_label.assert_called_once_with(250, NOT_NEEDED_VERIFIED_LABEL)
        assert second_result["closed"] == [250]
        assert second_result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    def test_rejected_label_clears_without_closing(
        self, fake_forge: MagicMock, tmp_path
    ):
        path = tmp_path / "state.json"
        self._state_with(
            PendingNotNeededReview(
                issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
            ),
            path=path,
        )
        fake_forge.get_issue_labels.return_value = (NOT_NEEDED_REJECTED_LABEL,)

        result = process_pending_not_needed_reviews(path, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        fake_forge.remove_label.assert_called_once_with(250, NOT_NEEDED_REJECTED_LABEL)
        assert result["reopened"] == [250]
        assert load_not_needed_review_state(path).pending == []

    def test_neither_label_present_keeps_entry_pending(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#511: タイムアウト未満であれば従来通り保留のまま（回帰なし）。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = ("status:not-needed",)

        result = process_pending_not_needed_reviews(
            path, forge=fake_forge, now=100.0, timeout_seconds=3600.0
        )

        assert result["still_pending"] == 1
        assert result["timed_out"] == []
        assert load_not_needed_review_state(path).pending == [entry]
        fake_forge.add_label.assert_not_called()

    def test_timed_out_entry_escalates_to_human_review(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#511 再現テスト: どちらの結果ラベルも付かないままタイムアウトを
        超えた保留エントリが、`status:blocked-human-review`へ決定論的に遷移し、
        台帳から消費される（永久pendingの解消）こと。

        修正前(Red): `still_pending`が減らず、何サイクル回しても保持され続ける。
        修正後(Green): 上限超過後、エントリが終端状態へ遷移して台帳から消える。
        """
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = ("status:not-needed",)

        result = process_pending_not_needed_reviews(
            path, forge=fake_forge, now=1.0 + 3600.0 + 1.0, timeout_seconds=3600.0
        )

        fake_forge.add_label.assert_called_once_with(250, "status:blocked-human-review")
        fake_forge.remove_label.assert_called_once_with(250, "status:not-needed")
        fake_forge.add_comment.assert_called_once()
        assert result["timed_out"] == [250]
        assert result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    def test_timeout_boundary_is_exclusive(self, fake_forge: MagicMock, tmp_path):
        """境界値: 経過時間が上限ちょうどの場合はまだ超過とみなさない
        （`prune_run_state`等、本コードベースの他の窓判定と同じ、超過は
        厳密な`>`で判定する意味論）。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = ("status:not-needed",)

        result = process_pending_not_needed_reviews(
            path, forge=fake_forge, now=1.0 + 3600.0, timeout_seconds=3600.0
        )

        assert result["timed_out"] == []
        assert result["still_pending"] == 1
        fake_forge.add_label.assert_not_called()

    def test_timeout_uses_the_default_when_unspecified(
        self, fake_forge: MagicMock, tmp_path
    ):
        """呼び出し元がtimeout_secondsを指定しなければ、有限の既定値
        （`DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS`）が使われること。
        「終端がない経路を残さない」という#511の目的上、無指定でも必ず
        有限の上限で終端することを固定する。"""
        path = tmp_path / "state.json"
        very_old = PendingNotNeededReview(
            issue_number=250,
            subtask_id="plot-api-routes",
            dispatched_at=0.0,
        )
        self._state_with(very_old, path=path)
        fake_forge.get_issue_labels.return_value = ("status:not-needed",)

        result = process_pending_not_needed_reviews(
            path,
            forge=fake_forge,
            now=DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS + 1.0,
        )

        assert result["timed_out"] == [250]

    def test_escalation_removes_only_the_not_needed_label_actually_present(
        self, fake_forge: MagicMock, tmp_path
    ):
        """`apply_human_review_escalation`へ渡す除去対象は、実際に現在フェッチ
        できたラベルに含まれるものだけに絞る（無関係なラベルを誤って除去
        除去対象扱いしない）。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = (
            "status:not-needed",
            "priority:high",
        )

        process_pending_not_needed_reviews(
            path, forge=fake_forge, now=1.0 + 3600.0 + 1.0, timeout_seconds=3600.0
        )

        fake_forge.remove_label.assert_called_once_with(250, "status:not-needed")

    def test_comment_failure_still_consumes_the_entry(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#511: `status:blocked-human-review`の付与が確定した直後に台帳から
        消費する。付与後のコメント投稿だけが失敗しても、新ラベル自体は
        既にGitHub側で確定しているため、台帳へエントリを残して次サイクルへ
        持ち越してはいけない（残すと同じタイムアウト条件を再評価し続け、
        既に届いている`status:blocked-human-review`へ重複コメントを送る）。
        `#512`/PR#520で確立した`on_label_applied`の順序と同じ設計。"""
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.return_value = ("status:not-needed",)
        fake_forge.add_comment.side_effect = RuntimeError("gh api error")

        result = process_pending_not_needed_reviews(
            path, forge=fake_forge, now=1.0 + 3600.0 + 1.0, timeout_seconds=3600.0
        )

        # 新ラベルの付与自体は成功している。
        fake_forge.add_label.assert_called_once_with(250, "status:blocked-human-review")
        # コメント失敗で例外が送出されるため、timed_outへのappendより前で
        # 打ち切られる。ただし台帳の消費（on_label_applied）は既に確定済み。
        assert result["timed_out"] == []
        assert result["still_pending"] == 0
        assert load_not_needed_review_state(path).pending == []

    def test_label_polling_failure_keeps_entry_pending(
        self, fake_forge: MagicMock, tmp_path
    ):
        path = tmp_path / "state.json"
        entry = PendingNotNeededReview(
            issue_number=250, subtask_id="plot-api-routes", dispatched_at=1.0
        )
        self._state_with(entry, path=path)
        fake_forge.get_issue_labels.side_effect = RuntimeError("gh api error")

        result = process_pending_not_needed_reviews(path, forge=fake_forge)

        assert result["still_pending"] == 1
        assert load_not_needed_review_state(path).pending == [entry]

    def test_base_exception_mid_loop_preserves_full_pending_state(
        self, fake_forge: MagicMock, tmp_path
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

        fake_forge.get_issue_labels.side_effect = labels_side_effect

        with pytest.raises(KeyboardInterrupt):
            process_pending_not_needed_reviews(
                path, forge=fake_forge, now=100.0, timeout_seconds=3600.0
            )

        # 中断時でも、まだ消費していないエントリ（未処理・要再試行）は温存される。
        remaining = {p.issue_number for p in load_not_needed_review_state(path).pending}
        assert remaining == {100, 200}

    def test_base_exception_after_consuming_entry_drops_only_consumed(
        self, fake_forge: MagicMock, tmp_path
    ):
        """#226/PR#227レビュー: 割り込み前に passed で正常消費（クローズ＋ラベル削除）
        済みのエントリは、中断時の台帳保存でも除外され、後続の未処理エントリのみが
        残ること。消費済みエントリを台帳へ復帰させると、次サイクルでは完了ラベルが
        既に無いため永久pending化する（#205と同種のリーク）ため、これを防ぐ。
        """
        path = tmp_path / "state.json"
        consumed = PendingNotNeededReview(
            issue_number=100, subtask_id="consumed", dispatched_at=1.0
        )
        interrupted = PendingNotNeededReview(
            issue_number=200, subtask_id="interrupted", dispatched_at=1.0
        )
        self._state_with(consumed, interrupted, path=path)
        fake_forge.get_issue_state.return_value = "OPEN"

        def labels_side_effect(issue_number):
            if issue_number == 100:
                return (NOT_NEEDED_VERIFIED_LABEL,)
            raise KeyboardInterrupt()

        fake_forge.get_issue_labels.side_effect = labels_side_effect

        with pytest.raises(KeyboardInterrupt):
            process_pending_not_needed_reviews(path, forge=fake_forge)

        # #100 はクローズ＋passedラベル削除まで成功済み（消費済み）なので台帳から除外し、
        # 割り込みで未処理の #200 のみを残す。
        fake_forge.close_issue.assert_called_once()
        fake_forge.remove_label.assert_called_once_with(100, NOT_NEEDED_VERIFIED_LABEL)
        remaining = {p.issue_number for p in load_not_needed_review_state(path).pending}
        assert remaining == {200}
