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
    NoticeDelivery,
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
from orchestune.pr_link_notice_trust import (
    REASON_COLLABORATORS_UNAVAILABLE,
    REASON_IDENTITY_UNRESOLVED,
    NoticePresence,
    TrustContext,
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


def _comment(body: str, author: str = "bot") -> dict[str, Any]:
    return {
        "body": body,
        "author": author,
        "user": {"login": author},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


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
    def test_absent_when_no_comments(self):
        trust = TrustContext(MagicMock())
        assert has_notice([], KIND_CREATED, 456, trust=trust) is NoticePresence.ABSENT

    def test_present_when_marker_posted_by_self(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "my-bot"
        trust = TrustContext(forge)
        comments = [
            _comment(render_created_notice(456, "parent/issue-100"), author="my-bot")
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.PRESENT
        )
        forge.list_notice_trusted_authors.assert_not_called()

    def test_casefold_match_with_self(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "My-Bot"
        trust = TrustContext(forge)
        comments = [
            _comment(render_created_notice(456, "parent/issue-100"), author="my-bot")
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.PRESENT
        )

    def test_does_not_strip_bot_suffix_when_comparing_self(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "github-actions[bot]"
        forge.list_notice_trusted_authors.return_value = frozenset()
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="github-actions",
            )
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.ABSENT
        )

    def test_present_when_marker_posted_by_collaborator(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "my-bot"
        forge.list_notice_trusted_authors.return_value = frozenset({"collab-user"})
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="collab-user",
            )
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.PRESENT
        )

    def test_absent_when_marker_posted_by_untrusted_third_party(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "my-bot"
        forge.list_notice_trusted_authors.return_value = frozenset({"collab-user"})
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="attacker",
            )
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.ABSENT
        )

    def test_unknown_when_identity_unresolved_and_candidate_present(self):
        forge = MagicMock()
        forge.get_authenticated_user.side_effect = RuntimeError(
            "Token cannot get /user"
        )
        forge.list_notice_trusted_authors.return_value = frozenset({"collab-user"})
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="attacker",
            )
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.UNKNOWN
        )
        assert trust.last_deferred_reason == REASON_IDENTITY_UNRESOLVED

    def test_unknown_when_collaborators_api_fails(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "my-bot"
        forge.list_notice_trusted_authors.side_effect = RuntimeError(
            "Collaborators API 500"
        )
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="someone-else",
            )
        ]

        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.UNKNOWN
        )
        assert trust.last_deferred_reason == REASON_COLLABORATORS_UNAVAILABLE

    def test_present_when_trusted_follows_unknown_candidate(self):
        forge = MagicMock()
        forge.get_authenticated_user.side_effect = RuntimeError("No GET /user")
        forge.list_notice_trusted_authors.return_value = frozenset({"trusted-collab"})
        trust = TrustContext(forge)
        comments = [
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="random-user",
            )
        ]
        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.UNKNOWN
        )
        # 信頼済み候補が後ろにあれば PRESENT
        comments.append(
            _comment(
                render_created_notice(456, "parent/issue-100"),
                author="trusted-collab",
            )
        )
        assert (
            has_notice(comments, KIND_CREATED, 456, trust=trust)
            is NoticePresence.PRESENT
        )

    def test_false_for_other_pr_or_kind(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "my-bot"
        trust = TrustContext(forge)
        comments = [
            _comment(render_created_notice(456, "parent/issue-100"), author="my-bot")
        ]

        assert (
            has_notice(comments, KIND_CREATED, 457, trust=trust)
            is NoticePresence.ABSENT
        )
        assert (
            has_notice(comments, KIND_MERGED, 456, trust=trust) is NoticePresence.ABSENT
        )

    def test_tolerates_comments_without_body(self):
        trust = TrustContext(MagicMock())
        assert (
            has_notice([{"author": "bot"}], KIND_CREATED, 456, trust=trust)
            is NoticePresence.ABSENT
        )

    def test_matches_marker_stored_with_crlf(self):
        forge = MagicMock()
        forge.get_authenticated_user.return_value = "bot"
        trust = TrustContext(forge)
        body = render_created_notice(456, "parent/issue-100").replace("\n", "\r\n")

        assert (
            has_notice([_comment(body)], KIND_CREATED, 456, trust=trust)
            is NoticePresence.PRESENT
        )


class TestNotifyPrCreated:
    def test_posts_once(self, fake_forge: MagicMock):
        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is True

        fake_forge.create_issue_comment.assert_called_once()
        issue_number, body = fake_forge.create_issue_comment.call_args.args[:2]
        assert issue_number == 12
        assert notice_marker(KIND_CREATED, 456) in body

    def test_skips_when_already_notified_by_self(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment(
            12, render_created_notice(456, "parent/issue-100"), author="bot"
        )
        fake_forge.create_issue_comment.reset_mock()

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False
        fake_forge.create_issue_comment.assert_not_called()

    def test_posts_when_existing_marker_is_untrusted(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment(
            12, render_created_notice(456, "parent/issue-100"), author="attacker"
        )
        fake_forge.create_issue_comment.reset_mock()

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is True
        fake_forge.create_issue_comment.assert_called_once()

    def test_fails_closed_when_comments_cannot_be_read(self, fake_forge: MagicMock):
        fake_forge.list_all_issue_comments.side_effect = RuntimeError("API unavailable")

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False
        fake_forge.create_issue_comment.assert_not_called()

    def test_fails_closed_when_identity_unresolved_with_candidate(
        self, fake_forge: MagicMock
    ):
        fake_forge.create_issue_comment(
            12, render_created_notice(456, "parent/issue-100"), author="someone"
        )
        fake_forge.create_issue_comment.reset_mock()
        fake_forge.get_authenticated_user.side_effect = RuntimeError("No GET /user")

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False
        fake_forge.create_issue_comment.assert_not_called()

    def test_comment_failure_is_reported_but_not_raised(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment.side_effect = RuntimeError("API unavailable")

        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is False

    def test_posts_when_no_candidates_even_if_identity_fails(
        self, fake_forge: MagicMock
    ):
        fake_forge.get_authenticated_user.side_effect = RuntimeError("No GET /user")
        assert notify_pr_created(fake_forge, 12, 456, "parent/issue-100") is True
        fake_forge.create_issue_comment.assert_called_once()


class TestEnsurePrMergedNotice:
    def test_posts_when_not_yet_notified(self, fake_forge: MagicMock):
        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.POSTED
        assert delivery.can_close is True

        fake_forge.create_issue_comment.assert_called_once()
        issue_number, body = fake_forge.create_issue_comment.call_args.args[:2]
        assert issue_number == 12
        assert notice_marker(KIND_MERGED, 456) in body

    def test_already_present_when_notified_by_self(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment(
            12, render_merged_notice(456, "parent/issue-100"), author="bot"
        )
        fake_forge.create_issue_comment.reset_mock()

        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.ALREADY_PRESENT
        assert delivery.can_close is True
        fake_forge.create_issue_comment.assert_not_called()

    def test_posts_when_existing_marker_is_untrusted(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment(
            12, render_merged_notice(456, "parent/issue-100"), author="attacker"
        )
        fake_forge.create_issue_comment.reset_mock()

        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.POSTED
        assert delivery.can_close is True
        fake_forge.create_issue_comment.assert_called_once()

    def test_deferred_read_when_comments_cannot_be_read(self, fake_forge: MagicMock):
        fake_forge.list_all_issue_comments.side_effect = RuntimeError("API unavailable")

        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.DEFERRED_READ
        assert delivery.can_close is False
        fake_forge.create_issue_comment.assert_not_called()

    def test_deferred_trust_when_candidate_marker_with_unresolved_identity(
        self, fake_forge: MagicMock
    ):
        fake_forge.create_issue_comment(
            12, render_merged_notice(456, "parent/issue-100"), author="someone"
        )
        fake_forge.create_issue_comment.reset_mock()
        fake_forge.get_authenticated_user.side_effect = RuntimeError("No GET /user")

        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.DEFERRED_TRUST
        assert delivery.can_close is False
        fake_forge.create_issue_comment.assert_not_called()

    def test_deferred_write_when_comment_fails_to_post(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment.side_effect = RuntimeError("API write failed")

        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.DEFERRED_WRITE
        assert delivery.can_close is False

    def test_posts_when_no_candidates_even_if_identity_fails(
        self, fake_forge: MagicMock
    ):
        fake_forge.get_authenticated_user.side_effect = RuntimeError("No GET /user")
        delivery = ensure_pr_merged_notice(fake_forge, 12, 456, "parent/issue-100")
        assert delivery is NoticeDelivery.POSTED
        assert delivery.can_close is True
        fake_forge.create_issue_comment.assert_called_once()


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
        fake_forge.create_issue_comment.assert_called_once()

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
        fake_forge.create_issue_comment.assert_not_called()

    def test_skips_issues_outside_the_known_set(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="claude/issue-77-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
            closes_issue_numbers=(77,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.create_issue_comment.assert_not_called()

    def test_skips_prs_without_resolvable_issue(self, fake_forge: MagicMock):
        pr = make_pr(
            456,
            head_ref="integration/temp-parent-issue-100-run",
            base_ref="parent/issue-100",
            is_cross_repository=False,
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.create_issue_comment.assert_not_called()

    def test_does_not_repost_for_an_already_notified_pr(self, fake_forge: MagicMock):
        fake_forge.create_issue_comment(
            12, render_created_notice(456, "parent/issue-100"), author="bot"
        )
        fake_forge.create_issue_comment.reset_mock()
        pr = make_pr(
            456,
            head_ref="claude/issue-12-task-a",
            base_ref="parent/issue-100",
            is_cross_repository=False,
            closes_issue_numbers=(12,),
        )

        assert notify_open_pr_links(fake_forge, [pr], {12: "parent/issue-100"}) == []
        fake_forge.create_issue_comment.assert_not_called()

    def test_one_pr_failure_does_not_stop_the_remaining_prs(
        self, fake_forge: MagicMock
    ):
        fake_forge.create_issue_comment.side_effect = [
            RuntimeError("API unavailable"),
            None,
        ]
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
            parent_issue_number=100,
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
            "orchestune.dispatch.phase_rebase.list_remote_branches",
            autospec=True,
            return_value=[],
        ):
            run_dispatch_cycle(config)

    def test_applies_notice_for_parent_targeted_pr(
        self, tmp_path: Any, fake_forge: MagicMock
    ):
        self._run_cycle(self._config(tmp_path, apply=True), fake_forge)

        bodies = [
            call.args[1] for call in fake_forge.create_issue_comment.call_args_list
        ]
        assert any(notice_marker(KIND_CREATED, 456) in body for body in bodies)

    def test_dry_run_posts_nothing(self, tmp_path: Any, fake_forge: MagicMock):
        self._run_cycle(self._config(tmp_path, apply=False), fake_forge)

        fake_forge.create_issue_comment.assert_not_called()


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
        fake_forge.create_issue_comment.assert_not_called()


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
        fake_forge.create_issue_comment.assert_not_called()

    def test_skips_pr_with_unknown_identity(self, fake_forge: MagicMock):
        assert (
            notify_open_pr_links(
                fake_forge, [self._fork_pr(None)], {12: "parent/issue-100"}
            )
            == []
        )
        fake_forge.create_issue_comment.assert_not_called()

    def test_notifies_upstream_pr(self, fake_forge: MagicMock):
        events = notify_open_pr_links(
            fake_forge, [self._fork_pr(False)], {12: "parent/issue-100"}
        )

        assert [event["issue_number"] for event in events] == [12]
