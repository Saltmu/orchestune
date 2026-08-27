"""#681: 最終統合PR本文（`Closes #` + 子Issue/サブタスクPR一覧）の生成。"""

from __future__ import annotations

import pytest

from orchestune.integrator.final_pr_body import (
    ChildSummary,
    collect_child_summaries,
    render_final_pr_body,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord, ReviewSummary


def _child(number: int = 101, title: str = "[FEAT] サブタスクA") -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body="",
        labels=(),
        created_at="2026-01-01T00:00:00Z",
        state="CLOSED",
    )


def _subtask_pr(
    number: int = 201,
    *,
    head_ref: str = "claude/issue-101-task-a",
    base_ref: str = "parent/issue-100",
    closes: tuple[int, ...] = (101,),
    state: str = "MERGED",
    is_cross_repository: bool | None = False,
    review_decision: str = "",
) -> PrRecord:
    return PrRecord(
        number=number,
        head_ref=head_ref,
        changed_files=(),
        closes_issue_numbers=closes,
        state=state,
        base_ref=base_ref,
        is_cross_repository=is_cross_repository,
        review_decision=review_decision,
    )


def _outcome_comment(
    record: OutcomeRecord, created_at: str = "2026-01-02T00:00:00Z"
) -> dict[str, str]:
    return {"body": record.render(), "created_at": created_at}


class TestRenderFinalPrBody:
    def test_body_opens_with_the_parent_closing_keyword(self):
        """受け入れ基準: 最終PR本文に`Closes #`が含まれること。

        GitHubのDevelopment連携・マージ時の自動クローズを働かせるため、
        本文の先頭行に置く。"""
        body = render_final_pr_body(100, [])

        assert body.splitlines()[0] == "Closes #100"

    def test_table_lists_child_issue_title_pr_and_review(self):
        body = render_final_pr_body(
            100,
            [
                ChildSummary(
                    issue_number=101,
                    title="[FEAT] サブタスクA",
                    pr_numbers=(201,),
                    review="done (claude / approved)",
                )
            ],
        )

        assert "| #101 | [FEAT] サブタスクA | #201 | done (claude / approved) |" in body

    def test_child_issue_number_stays_unescaped_so_github_links_it(self):
        """子Issue番号は`Closes`等のキーワードを伴わない素の`#N`として書き、
        GitHubの相互参照リンクが張られるようにする。"""
        body = render_final_pr_body(100, [ChildSummary(issue_number=101, title="t")])

        assert "| #101 |" in body
        assert "Closes #101" not in body

    def test_multiple_subtask_prs_are_listed_in_one_row(self):
        body = render_final_pr_body(
            100,
            [ChildSummary(issue_number=101, title="t", pr_numbers=(201, 205))],
        )

        assert "| #201, #205 |" in body

    def test_missing_pr_and_review_render_a_placeholder(self):
        body = render_final_pr_body(100, [ChildSummary(issue_number=101, title="t")])

        assert "| #101 | t | — | — |" in body

    def test_table_is_omitted_when_no_summary_could_be_built(self):
        """縮退時（子Issueを1件も解決できなかった場合）でもPR本文自体は成立させる。"""
        body = render_final_pr_body(100, [])

        assert "Closes #100" in body
        assert "| 子Issue |" not in body

    def test_issue_reference_in_a_child_title_is_escaped(self):
        """Reproducer(セキュリティ境界): 子Issueタイトルに`fixes #45`のような
        クローズキーワードが含まれていると、エスケープしなければ最終統合PRの
        マージで**無関係なIssue #45まで自動クローズ**されてしまう。"""
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="fixes #45 の回帰対応")]
        )

        assert "fixes #45" not in body
        assert r"fixes \#45 の回帰対応" in body

    def test_pipe_in_a_child_title_does_not_break_the_table(self):
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="a | b")]
        )

        assert r"| #101 | a \| b | — | — |" in body

    def test_backslash_in_a_child_title_is_escaped_before_the_pipe(self):
        """`a\\|b`をそのまま出すと`\\\\`が「エスケープされた円記号」と解釈され、
        続く`|`が生のセル区切りとして働いて表が壊れる。"""
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title=r"a\|b")]
        )

        assert r"| #101 | a\\\|b | — | — |" in body

    def test_newlines_in_a_child_title_are_collapsed(self):
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="a\nb\r\nc")]
        )

        assert "| #101 | a b c | — | — |" in body

    def test_review_text_is_escaped_too(self):
        """レビュー結果もbot由来の文字列を含みうるため、同じ規則で無害化する。"""
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="t", review="closes #9")]
        )

        assert "closes #9" not in body
        assert r"closes \#9" in body

    def test_url_form_issue_reference_in_a_child_title_is_neutralized(self):
        """PR#690レビュー対応(Codex P2): GitHubはURL形式のクローズ参照も
        受け付けるため、`#`のエスケープだけでは
        `Fixes https://github.com/owner/repo/issues/45`が素通りし、最終統合PRの
        マージで無関係な#45がクローズされてしまう。"""
        body = render_final_pr_body(
            100,
            [
                ChildSummary(
                    issue_number=101,
                    title="Fixes https://github.com/Saltmu/orchestune/issues/45",
                )
            ],
        )

        assert "https://github.com/Saltmu/orchestune/issues/45" not in body
        assert r"Fixes https:\/\/github.com/Saltmu/orchestune/issues/45" in body

    def test_url_form_pull_request_reference_is_neutralized_too(self):
        body = render_final_pr_body(
            100,
            [
                ChildSummary(
                    issue_number=101,
                    title="closes http://www.github.com/o/r/pull/7",
                )
            ],
        )

        assert r"closes http:\/\/www.github.com/o/r/pull/7" in body

    def test_unrelated_url_in_a_child_title_is_left_alone(self):
        """無害化の対象はGitHubのIssue/PR参照URLだけ。関係のないURLまで
        壊すと、タイトルの情報が読みにくくなるだけで安全性には寄与しない。"""
        body = render_final_pr_body(
            100,
            [ChildSummary(issue_number=101, title="see https://example.com/issues/45")],
        )

        assert "see https://example.com/issues/45" in body

    def test_gh_form_issue_reference_in_a_child_title_is_neutralized(self):
        """`GH-45`もGitHubが受け付けるクローズ参照形式。"""
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="fixes GH-45 の回帰対応")]
        )

        assert "GH-45" not in body
        assert r"fixes GH\-45 の回帰対応" in body

    def test_gh_prefixed_word_without_a_number_is_left_alone(self):
        body = render_final_pr_body(
            100, [ChildSummary(issue_number=101, title="GH-Actions の設定")]
        )

        assert "GH-Actions の設定" in body


class TestCollectChildSummaries:
    @pytest.fixture(autouse=True)
    def _inject_forge(self, fake_forge):
        self.forge = fake_forge

    def test_matches_a_merged_subtask_pr_by_closing_reference(self):
        self.forge.list_prs.return_value = [_subtask_pr(201, head_ref="feature/x")]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == (201,)

    def test_matches_a_merged_subtask_pr_by_head_branch_name(self):
        """エージェントが`Closes`記法を書かずに起票したPRでも解決できること。"""
        self.forge.list_prs.return_value = [
            _subtask_pr(201, head_ref="claude/issue-101-task-a", closes=())
        ]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == (201,)

    def test_ignores_a_pr_that_is_not_merged(self):
        self.forge.list_prs.return_value = [_subtask_pr(201, state="OPEN")]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == ()

    def test_ignores_a_cross_repository_pr(self):
        """#684と同じfail closed方針: forkのPRを「このIssueのPR」として
        権威ある体裁で最終統合PR本文へ書き込ませない。"""
        self.forge.list_prs.return_value = [
            _subtask_pr(666, is_cross_repository=True),
            _subtask_pr(667, is_cross_repository=None),
        ]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == ()

    def test_ignores_a_pr_targeting_another_parent_branch(self):
        self.forge.list_prs.return_value = [
            _subtask_pr(201, base_ref="parent/issue-200")
        ]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == ()

    def test_prefers_the_outcome_record_posted_on_the_subtask_pr(self):
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        record = OutcomeRecord(
            result="done",
            issue=101,
            pr=201,
            review=ReviewSummary(bot="codex", rounds=2, verdict="approved"),
        )
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(record)] if number == 201 else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "done (codex / approved / 2ラウンド)"

    def test_falls_back_to_the_outcome_record_on_the_child_issue(self):
        self.forge.list_prs.return_value = []
        record = OutcomeRecord(result="not-needed", issue=101)
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(record)] if number == 101 else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "not-needed"

    def test_rejects_an_outcome_record_naming_another_issue(self):
        """Codexレビュー(P2) Reproducer: PRが複数Issueを閉じる場合や古い
        レコードが貼り直された場合、そのPRのコメントには別タスクのOutcome
        Recordが載りうる。`issue`は契約上の識別子なので、一致しないレコードを
        この子Issueのレビュー結果として表示してはならない。"""
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        foreign = OutcomeRecord(result="done", issue=999, pr=201)
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(foreign)] if number == 201 else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "APPROVED"

    def test_rejects_an_outcome_record_naming_another_pr(self):
        """同(P2): PRコメント上のレコードが別PRを名乗る場合も同様に弾く。"""
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        foreign = OutcomeRecord(result="done", issue=101, pr=999)
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(foreign)] if number == 201 else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "APPROVED"

    def test_picks_the_latest_record_that_identifies_this_child(self):
        """PR#690レビュー対応(Codex P2) Reproducer: 識別チェックを
        `parse_from_comments`の後段に置くと、「最新は別タスクのレコード、
        その1つ前がこの子の正しいレコード」というコメント欄で、パーサが先に
        最新1件へ絞ってしまうため正しいレコードが検討されずに捨てられる。
        絞り込みを先に行い「この子を指すレコードのうち最新」を選ぶこと。"""
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        mine = OutcomeRecord(
            result="done",
            issue=101,
            pr=201,
            review=ReviewSummary(bot="codex", rounds=2, verdict="approved"),
        )
        foreign = OutcomeRecord(result="done", issue=999, pr=201)
        self.forge.list_comments.side_effect = lambda number: (
            [
                _outcome_comment(mine, created_at="2026-01-02T00:00:00Z"),
                _outcome_comment(foreign, created_at="2026-01-03T00:00:00Z"),
            ]
            if number == 201
            else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "done (codex / approved / 2ラウンド)"

    def test_accepts_an_outcome_record_that_omits_the_pr_field(self):
        """境界: `pr`は任意フィールドなので、未設定を不一致として弾かない
        （弾くと`pr`を省略した正当なレコードが全て失われる）。"""
        self.forge.list_prs.return_value = [_subtask_pr(201)]
        record = OutcomeRecord(result="done", issue=101)
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(record)] if number == 201 else []
        )

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "done"

    def test_rejects_a_foreign_outcome_record_on_the_child_issue_itself(self):
        """同(P2): 子Issue側のコメントに載った別タスクのレコードも弾く。"""
        self.forge.list_prs.return_value = []
        foreign = OutcomeRecord(result="done", issue=999)
        self.forge.list_comments.return_value = [_outcome_comment(foreign)]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == ""

    def test_blocked_outcome_reports_its_reason(self):
        self.forge.list_prs.return_value = []
        record = OutcomeRecord(
            result="blocked", issue=101, reason="base-branch-red", attempt=3
        )
        self.forge.list_comments.return_value = [_outcome_comment(record)]

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "blocked (base-branch-red)"

    def test_falls_back_to_the_pr_review_decision_without_an_outcome_record(self):
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        self.forge.list_comments.return_value = []

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == "APPROVED"

    def test_review_is_empty_when_no_signal_is_available(self):
        self.forge.list_prs.return_value = []
        self.forge.list_comments.return_value = []

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].review == ""

    def test_summaries_are_sorted_by_child_issue_number(self):
        """`find_children_by_parent`はnative+metadataの連結順で返すため、
        本文が毎サイクル並び替わらないよう決定論的に整列する。"""
        summaries = collect_child_summaries(
            self.forge, 100, [_child(103), _child(101), _child(102)]
        )

        assert [s.issue_number for s in summaries] == [101, 102, 103]

    def test_list_prs_failure_yields_no_summaries_at_all(self):
        """Codexレビュー(P2) Reproducer: `list_prs`が一時障害で落ちたとき、
        `prs=[]`として行を組み立てると、実際にはPRがある子Issueの
        サブタスクPR欄が`—`になる。この「PRが無かった」と読める偽の行は、
        再利用PRでは`if summaries`を通過して投稿済みの正しい表を上書きし、
        新規PRでは誤情報をそのまま掲載してしまう。行を作らないことで
        既存の縮退ガード（テーブル省略・本文非更新）へ倒す。"""
        self.forge.list_prs.side_effect = RuntimeError("transient API failure")

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries == []

    def test_list_comments_failure_is_non_fatal(self):
        self.forge.list_prs.return_value = [
            _subtask_pr(201, review_decision="APPROVED")
        ]
        self.forge.list_comments.side_effect = RuntimeError("transient API failure")

        summaries = collect_child_summaries(self.forge, 100, [_child(101)])

        assert summaries[0].pr_numbers == (201,)
        assert summaries[0].review == "APPROVED"

    def test_child_issue_comments_are_not_fetched_once_the_pr_answered(self):
        """APIコスト制限: Outcome Recordはスキル契約上PRコメントが第一の
        投稿先であるため、そこで解決できた子Issueのコメントは読みに行かない。"""
        record = OutcomeRecord(result="done", issue=101, pr=201)
        self.forge.list_prs.return_value = [_subtask_pr(201)]
        self.forge.list_comments.side_effect = lambda number: (
            [_outcome_comment(record)] if number == 201 else []
        )

        collect_child_summaries(self.forge, 100, [_child(101)])

        assert [call.args[0] for call in self.forge.list_comments.call_args_list] == [
            201
        ]
