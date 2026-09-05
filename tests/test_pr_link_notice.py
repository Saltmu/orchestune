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
from orchestune.models import PrRecord
from orchestune.pr_link_notice import (
    KIND_CREATED,
    KIND_MERGED,
    ensure_pr_merged_notice,
    extract_issue_numbers_from_pr,
    has_notice,
    notice_expected_bases,
    notice_marker,
    notify_open_pr_links,
    notify_pr_created,
    pr_matches_issue,
    render_created_notice,
    render_merged_notice,
)
from tests.conftest import make_issue, make_pr, make_task


class TestExtractIssueNumbersFromPr:
    def test_extracts_from_closes_issue_numbers(self):
        pr = PrRecord(
            number=1, head_ref="feat/x", changed_files=(), closes_issue_numbers=(42, 43)
        )
        assert extract_issue_numbers_from_pr(pr) == {42, 43}

    def test_extracts_from_agent_issue_head_branch(self):
        pr = PrRecord(
            number=1,
            head_ref="codex/issue-709-guarded-repair-rollout",
            changed_files=(),
        )
        assert extract_issue_numbers_from_pr(pr) == {709}

    def test_extracts_from_dot_separated_head_branch(self):
        pr = PrRecord(number=1, head_ref="codex/issue-709.rollout", changed_files=())
        assert extract_issue_numbers_from_pr(pr) == {709}

    def test_extracts_from_type_issue_head_branch(self):
        pr = PrRecord(number=1, head_ref="fix/issue-730-test", changed_files=())
        assert extract_issue_numbers_from_pr(pr) == {730}

    def test_extracts_from_title(self):
        pr = PrRecord(
            number=1,
            head_ref="feat/other",
            changed_files=(),
            title="fix: resolve #739 and #740",
        )
        assert extract_issue_numbers_from_pr(pr) == {739, 740}

    def test_extracts_from_body(self):
        pr = PrRecord(
            number=1,
            head_ref="feat/other",
            changed_files=(),
            body="This fixes #739\nCloses #740",
        )
        assert extract_issue_numbers_from_pr(pr) == {739, 740}


class TestPrMatchesIssue:
    def test_matches_by_branch_issue_number(self):
        pr = PrRecord(
            number=1,
            head_ref="codex/issue-709-guarded-repair-rollout",
            changed_files=(),
        )
        assert pr_matches_issue(pr, 709) is True
        assert pr_matches_issue(pr, 710) is False

    def test_matches_by_subtask_id_and_issue_number(self):
        pr = PrRecord(
            number=1, head_ref="custom/709-guarded-repair-rollout", changed_files=()
        )
        assert pr_matches_issue(pr, 709, subtask_id="guarded-repair-rollout") is True
        assert pr_matches_issue(pr, 710, subtask_id="guarded-repair-rollout") is False


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


class TestEnsurePrMergedNotice:
    def test_posts_when_not_yet_notified(self, fake_forge: MagicMock):
        assert ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100") is True

        fake_forge.add_comment.assert_called_once()
        issue_number, body = fake_forge.add_comment.call_args.args
        assert issue_number == 12
        assert notice_marker(KIND_MERGED, 456) in body

    def test_reports_success_without_reposting_when_already_notified(
        self, fake_forge: MagicMock
    ):
        fake_forge.list_comments.return_value = [
            _comment(render_merged_notice(456, "parent/issue-100"))
        ]

        assert ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100") is True
        fake_forge.add_comment.assert_not_called()

    def test_posts_anyway_when_comments_cannot_be_read(self, fake_forge: MagicMock):
        # マージ通知はIssueをクローズする直前の書き込みで、クローズ後は統合対象から
        # 外れて再試行されない。判定不能なら重複の可能性を受け入れて投稿する。
        fake_forge.list_comments.side_effect = RuntimeError("API unavailable")

        assert ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100") is True
        fake_forge.add_comment.assert_called_once()

    def test_reports_failure_when_the_comment_cannot_be_posted(
        self, fake_forge: MagicMock
    ):
        fake_forge.add_comment.side_effect = RuntimeError("API unavailable")

        assert ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100") is False


class TestNotifyOpenPrLinks:
    def test_notifies_issue_referenced_by_closes(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
            closes_issue_numbers=(12,),
        )

        events = notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"})

        assert events == [{"issue_number": 12, "pr_number": 456, "kind": KIND_CREATED}]
        fake_forge.add_comment.assert_called_once()

    def test_resolves_issue_from_head_branch_without_closes_reference(
        self, fake_forge: MagicMock
    ):
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
        )

        events = notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"})

        assert [event["issue_number"] for event in events] == [12]

    def test_skips_prs_targeting_the_default_branch(self, fake_forge: MagicMock):
        # main宛てのPRはGitHubが自動リンクするため、補完コメントは不要。
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="main",
            is_cross_repository=False,
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.add_comment.assert_not_called()

    def test_skips_issues_outside_the_known_set(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-77-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
            closes_issue_numbers=(77,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.add_comment.assert_not_called()

    def test_skips_prs_without_resolvable_issue(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="integration/temp-parent-issue-100-run",
            base_ref="parent/issue-100",
            is_cross_repository=False,
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.add_comment.assert_not_called()

    def test_does_not_repost_for_an_already_notified_pr(self, fake_forge: MagicMock):
        fake_forge.list_comments.return_value = [
            _comment(render_created_notice(456, "parent/issue-100"))
        ]
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.add_comment.assert_not_called()

    def test_one_pr_failure_does_not_stop_the_remaining_prs(
        self, fake_forge: MagicMock
    ):
        fake_forge.add_comment.side_effect = [RuntimeError("API unavailable"), None]
        prs = [
            make_pr(
                456,
                head_ref="claude/issue-12-task-a",
                base_ref="parent/issue-100",
                is_cross_repository=False,
            ),
            make_pr(
                457,
                head_ref="claude/issue-13-task-b",
                base_ref="parent/issue-100",
                is_cross_repository=False,
            ),
        ]

        events = notify_open_pr_links(
            fake_forge, prs, {12: "parent/issue-100", 13: "parent/issue-100"}
        )

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
                is_cross_repository=False,
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


class TestNoticeExpectedBases:
    """通知の走査対象を、まだ統合が終わっていないタスクに限定し、そのタスクが
    ぶら下がる親ブランチ名を対応付ける。

    親ブランチ運用では完了済みタスクのPRが開いたまま残ることがあり、
    全件を毎サイクル走査すると`list_comments`の呼び出しが完了タスク数に比例して
    増え続ける。
    """

    def test_maps_open_tasks_to_their_parent_branch(self):
        tasks = [
            make_task(12, status_labels=("status:in-progress",), parent_number=100)
        ]

        assert notice_expected_bases(tasks) == {12: "parent/issue-100"}

    def test_includes_done_tasks_not_yet_integrated(self):
        # 完了直後にPRが作成された場合でも、作成通知を投稿する余地を残す。
        tasks = [make_task(12, status_labels=("status:done",), parent_number=100)]

        assert notice_expected_bases(tasks) == {12: "parent/issue-100"}

    def test_excludes_closed_issues(self):
        tasks = [
            make_task(
                12,
                status_labels=("status:done",),
                issue_state="CLOSED",
                parent_number=100,
            )
        ]

        assert notice_expected_bases(tasks) == {}

    def test_excludes_already_integrated_tasks(self):
        tasks = [
            make_task(
                12,
                status_labels=("status:done", "integration:included"),
                parent_number=100,
            )
        ]

        assert notice_expected_bases(tasks) == {}

    def test_excludes_tasks_without_a_parent(self):
        # 親が特定できないタスクは、PRのbaseと照合する基準を持たない。
        tasks = [
            make_task(12, status_labels=("status:in-progress",), parent_number=None)
        ]

        assert notice_expected_bases(tasks) == {}


class TestBaseBranchMustMatchTheTaskParent:
    """PR#684レビュー対応(Codex P2): `parent/`接頭辞だけでなく、そのIssueの
    親ブランチと完全に一致するPRだけを通知する。"""

    def test_skips_pr_targeting_another_parent_branch(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-200",
            is_cross_repository=False,
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.add_comment.assert_not_called()


class TestCrossRepositoryPrs:
    """PR#684レビュー対応(Codex P2): forkからのPRは対象Issueへ書き込ませない。"""

    def _fork_pr(self, is_cross_repository: bool | None):
        return make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=is_cross_repository,
            closes_issue_numbers=(12,),
        )

    def test_skips_fork_pr(self, fake_forge: MagicMock):
        # forkの投稿者は`parent/*`宛てに`claude/issue-{N}-...`というheadを
        # 名乗れるため、identityを確認せずに通知すると権威ある体裁の
        # コメントを他人のIssueへ書き込めてしまう。
        assert (
            notify_open_pr_links(
                fake_forge, [self._fork_pr(True)], {12: "parent/issue-100"}
            )
            == []
        )
        fake_forge.add_comment.assert_not_called()

    def test_skips_pr_with_unknown_identity(self, fake_forge: MagicMock):
        assert (
            notify_open_pr_links(
                fake_forge, [self._fork_pr(None)], {12: "parent/issue-100"}
            )
            == []
        )
        fake_forge.add_comment.assert_not_called()

    def test_notifies_upstream_pr(self, fake_forge: MagicMock):
        events = notify_open_pr_links(
            fake_forge, [self._fork_pr(False)], {12: "parent/issue-100"}
        )

        assert [event["issue_number"] for event in events] == [12]
