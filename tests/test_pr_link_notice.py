"""#676: 親ブランチ宛てPRのIssue側リンク通知コメント。

GitHubの`Closes #N`自動リンクは、PRのbaseが既定ブランチの場合しか働かない。
`parent/issue-*`宛てのPRについて、対象Issueへ相互リンクのコメントを1回だけ
投稿することを保証する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.pr_link_notice import (
    KIND_CREATED,
    KIND_MERGED,
    has_notice,
    merged_notice_if_new,
    notice_candidate_issue_numbers,
    notice_marker,
    notify_open_pr_links,
    notify_pr_created,
    render_created_notice,
    render_merged_notice,
)
from tests.conftest import make_issue, make_pr, make_task


def _comment(body: str) -> dict[str, Any]:
    return {"body": body, "author": "bot", "created_at": "2026-01-01T00:00:00+00:00"}


class TestNoticeRendering:
    def test_created_notice_carries_marker_pr_and_base(self):
        body = render_created_notice(456, "parent/issue-100")

        assert notice_marker(KIND_CREATED, 456) in body
        assert "#456" in body
        assert "parent/issue-100" in body

    def test_merged_notice_carries_marker_pr_and_base(self):
        body = render_merged_notice(456, "parent/issue-100")

        assert notice_marker(KIND_MERGED, 456) in body
        assert "#456" in body
        assert "parent/issue-100" in body

    def test_markers_differ_by_kind_and_pr_number(self):
        assert notice_marker(KIND_CREATED, 1) != notice_marker(KIND_MERGED, 1)
        assert notice_marker(KIND_CREATED, 1) != notice_marker(KIND_CREATED, 2)


class TestHasNotice:
    def test_true_when_marker_present(self):
        comments = [_comment(render_created_notice(456, "parent/issue-100"))]

        assert has_notice(comments, KIND_CREATED, 456) is True

    def test_false_for_other_pr_or_kind(self):
        comments = [_comment(render_created_notice(456, "parent/issue-100"))]

        assert has_notice(comments, KIND_CREATED, 457) is False
        assert has_notice(comments, KIND_MERGED, 456) is False

    def test_tolerates_comments_without_body(self):
        assert has_notice([{"author": "bot"}], KIND_CREATED, 456) is False

    def test_matches_marker_stored_with_crlf(self):
        # #664と同じ理由: GitHubから読み戻す本文はCRLFになり得る。
        body = render_created_notice(456, "parent/issue-100").replace("\n", "\r\n")

        assert has_notice([_comment(body)], KIND_CREATED, 456) is True


class TestNotifyPrCreated:
    def test_posts_once(self, fake_forge: MagicMock):
        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is True

        fake_forge.add_comment.assert_called_once()
        issue_number, body = fake_forge.add_comment.call_args.args
        assert issue_number == 12
        assert notice_marker(KIND_CREATED, 456) in body

    def test_skips_when_already_notified(self, fake_forge: MagicMock):
        fake_forge.list_comments.return_value = [
            _comment(render_created_notice(456, "parent/issue-100"))
        ]

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False
        fake_forge.add_comment.assert_not_called()

    def test_fails_closed_when_comments_cannot_be_read(self, fake_forge: MagicMock):
        # 既存コメントを確認できない状態で投稿すると多重投稿になり得る。
        # 投稿を見送り、次サイクルの再試行に委ねる。
        fake_forge.list_comments.side_effect = RuntimeError("API unavailable")

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False
        fake_forge.add_comment.assert_not_called()

    def test_comment_failure_is_reported_but_not_raised(self, fake_forge: MagicMock):
        fake_forge.add_comment.side_effect = RuntimeError("API unavailable")

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False


class TestMergedNoticeIfNew:
    def test_returns_notice_body_when_not_yet_posted(self, fake_forge: MagicMock):
        body = merged_notice_if_new(fake_forge, 12, 456, "parent/issue-100")

        assert body is not None
        assert notice_marker(KIND_MERGED, 456) in body

    def test_returns_none_when_already_posted(self, fake_forge: MagicMock):
        fake_forge.list_comments.return_value = [
            _comment(render_merged_notice(456, "parent/issue-100"))
        ]

        assert merged_notice_if_new(fake_forge, 12, 456, "parent/issue-100") is None


class TestNotifyOpenPrLinks:
    def test_notifies_issue_referenced_by_closes(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            closes_issue_numbers=(12,),
        )

        events = notify_open_pr_links(fake_forge, [pr], {12})

        assert events == [{"issue_number": 12, "pr_number": 456, "kind": KIND_CREATED}]
        fake_forge.add_comment.assert_called_once()

    def test_resolves_issue_from_head_branch_without_closes_reference(
        self, fake_forge: MagicMock
    ):
        pr = make_pr(
            456, head_ref="claude/issue-12-task-a", base_ref="parent/issue-100"
        )

        events = notify_open_pr_links(fake_forge, [pr], {12})

        assert [event["issue_number"] for event in events] == [12]

    def test_skips_prs_targeting_the_default_branch(self, fake_forge: MagicMock):
        # main宛てのPRはGitHubが自動リンクするため、補完コメントは不要。
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="main",
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12}) == []
        fake_forge.add_comment.assert_not_called()

    def test_skips_issues_outside_the_known_set(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-77-task-a",
            base_ref="parent/issue-100",
            closes_issue_numbers=(77,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12}) == []
        fake_forge.add_comment.assert_not_called()

    def test_skips_prs_without_resolvable_issue(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="integration/temp-parent-issue-100-run",
            base_ref="parent/issue-100",
        )

        assert notify_open_pr_links(fake_forge, [pr], {12}) == []
        fake_forge.add_comment.assert_not_called()

    def test_does_not_repost_for_an_already_notified_pr(self, fake_forge: MagicMock):
        fake_forge.list_comments.return_value = [
            _comment(render_created_notice(456, "parent/issue-100"))
        ]
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12}) == []
        fake_forge.add_comment.assert_not_called()

    def test_one_pr_failure_does_not_stop_the_remaining_prs(
        self, fake_forge: MagicMock
    ):
        fake_forge.add_comment.side_effect = [RuntimeError("API unavailable"), None]
        prs = [
            make_pr(
                456, head_ref="claude/issue-12-task-a", base_ref="parent/issue-100"
            ),
            make_pr(
                457, head_ref="claude/issue-13-task-b", base_ref="parent/issue-100"
            ),
        ]

        events = notify_open_pr_links(fake_forge, prs, {12, 13})

        assert [event["issue_number"] for event in events] == [13]


class TestDispatchCycleWiring:
    """ディスパッチャーがサイクル中に検知したPRから通知が投稿されること。"""

    def _config(self, tmp_path: Any, *, apply: bool) -> DispatcherConfig:
        return DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            max_concurrent=0,
            apply=apply,
        )

    def _run_cycle(self, config: DispatcherConfig, fake_forge: MagicMock) -> None:
        issue = make_issue(12, labels=("status:in-progress",), subtask_id="task-a")
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.side_effect = lambda label, **_: (
            [issue] if label == "status:in-progress" else []
        )
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [
            make_pr(
                456,
                head_ref="claude/issue-12-task-a",
                base_ref="parent/issue-100",
                closes_issue_numbers=(12,),
            )
        ]
        with patch(
            "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
        ):
            run_dispatch_cycle(config)

    def test_applies_notice_for_parent_targeted_pr(
        self, tmp_path: Any, fake_forge: MagicMock
    ):
        self._run_cycle(self._config(tmp_path, apply=True), fake_forge)

        bodies = [call.args[1] for call in fake_forge.add_comment.call_args_list]
        assert any(notice_marker(KIND_CREATED, 456) in body for body in bodies)

    def test_dry_run_posts_nothing(self, tmp_path: Any, fake_forge: MagicMock):
        self._run_cycle(self._config(tmp_path, apply=False), fake_forge)

        fake_forge.add_comment.assert_not_called()


class TestMergedNoticeFailsOpen:
    """マージ通知は「そのIssueに対する最後の書き込み」なので、作成通知とは
    逆に fail open にする。"""

    def test_returns_notice_when_comments_cannot_be_read(self, fake_forge: MagicMock):
        # クローズ後のIssueは以降のサイクルで統合対象から外れ、再試行されない。
        # 判定不能なまま通知を落とすとPRリンクが恒久的に失われるため、
        # 重複の可能性を受け入れてでも通知本文を返す。
        fake_forge.list_comments.side_effect = RuntimeError("API unavailable")

        body = merged_notice_if_new(fake_forge, 12, 456, "parent/issue-100")

        assert body is not None
        assert notice_marker(KIND_MERGED, 456) in body


class TestNoticeCandidateIssueNumbers:
    """通知の走査対象を、まだ統合が終わっていないタスクに限定する。

    親ブランチ運用では完了済みタスクのPRが開いたまま残ることがあり、
    全件を毎サイクル走査すると`list_comments`の呼び出しが完了タスク数に比例して
    増え続ける。
    """

    def test_includes_open_tasks(self):
        tasks = [make_task(12, status_labels=("status:in-progress",))]

        assert notice_candidate_issue_numbers(tasks) == {12}

    def test_includes_done_tasks_not_yet_integrated(self):
        # 完了直後にPRが作成された場合でも、作成通知を投稿する余地を残す。
        tasks = [make_task(12, status_labels=("status:done",))]

        assert notice_candidate_issue_numbers(tasks) == {12}

    def test_excludes_closed_issues(self):
        tasks = [make_task(12, status_labels=("status:done",), issue_state="CLOSED")]

        assert notice_candidate_issue_numbers(tasks) == set()

    def test_excludes_already_integrated_tasks(self):
        tasks = [make_task(12, status_labels=("status:done", "integration:included"))]

        assert notice_candidate_issue_numbers(tasks) == set()
