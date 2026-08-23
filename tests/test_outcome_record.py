"""#548: `orchestune:outcome`完了宣言レコードのrender/parse往復変換テスト。"""

from __future__ import annotations

from orchestune.outcome_record import (
    OUTCOME_MARKER,
    OutcomeRecord,
    ReviewSummary,
    parse_from_comments,
)


class TestRenderParseRoundTrip:
    def test_minimal_done_record_round_trips(self):
        record = OutcomeRecord(result="done", issue=548, pr=560)
        body = record.render()
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) == record

    def test_not_needed_record_round_trips(self):
        record = OutcomeRecord(result="not-needed", issue=548, pr=None)
        body = record.render()
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) == record

    def test_blocked_record_with_reason_round_trips(self):
        record = OutcomeRecord(
            result="blocked",
            issue=548,
            pr=None,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=2,
        )
        body = record.render()
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) == record

    def test_full_record_with_review_and_regressions_round_trips(self):
        record = OutcomeRecord(
            result="done",
            issue=548,
            pr=560,
            review=ReviewSummary(bot="claude", rounds=2, verdict="lgtm"),
            ci="passing",
            baseline_regressions=("tests/test_flaky.py::test_x",),
        )
        body = record.render()
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) == record

    def test_rendered_body_contains_marker(self):
        record = OutcomeRecord(result="done", issue=1, pr=2)
        assert OUTCOME_MARKER in record.render()


class TestParseFromCommentsLatestWins:
    def test_picks_comment_with_max_created_at(self):
        older = OutcomeRecord(result="blocked", issue=1, reason="base-branch-red")
        newer = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [
            {"body": older.render(), "created_at": "2026-08-20T00:00:00Z"},
            {"body": newer.render(), "created_at": "2026-08-21T00:00:00Z"},
        ]
        assert parse_from_comments(comments) == newer

    def test_order_in_list_does_not_matter(self):
        older = OutcomeRecord(result="blocked", issue=1, reason="base-branch-red")
        newer = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [
            {"body": newer.render(), "created_at": "2026-08-21T00:00:00Z"},
            {"body": older.render(), "created_at": "2026-08-20T00:00:00Z"},
        ]
        assert parse_from_comments(comments) == newer


class TestParseFromCommentsFailClosed:
    def test_no_comments_returns_none(self):
        assert parse_from_comments([]) is None

    def test_no_marker_returns_none(self):
        comments = [{"body": "just a regular comment", "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_marker_without_fence_returns_none(self):
        comments = [{"body": f"{OUTCOME_MARKER}\nno json here", "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_malformed_json_returns_none(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done",\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_unclosed_fence_returns_none(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": 1}}\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_json_array_instead_of_object_returns_none(self):
        body = f"{OUTCOME_MARKER}\n```json\n[1, 2, 3]\n```\n"
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_invalid_result_value_returns_none(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "maybe", "issue": 1}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_missing_issue_returns_none(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done"}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_blocked_without_reason_returns_none(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "blocked", "issue": 1}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_blocked_with_unknown_reason_returns_none(self):
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "blocked", "issue": 1, "reason": "flaky-network"}\n```\n'
        )
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_bool_issue_value_rejected(self):
        """`bool`はPythonでは`int`のサブクラスであり、issue=Trueがissue=1として
        誤って受理されるのを防ぐ。"""
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": true}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_non_string_comment_body_is_skipped_not_raised(self):
        comments = [{"body": None, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_comment_missing_body_key_is_skipped_not_raised(self):
        comments = [{"created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_duplicate_marker_in_single_comment_does_not_raise(self):
        record = OutcomeRecord(result="done", issue=1, pr=2)
        body = record.render() + "\n" + record.render()
        comments = [{"body": body, "created_at": "x"}]
        # 例外を送出しないことが主な要件。マーカー重複時にどちらのレコードが
        # 採用されるかは規定しない。
        result = parse_from_comments(comments)
        assert result is None or result == record

    def test_falsy_review_value_is_rejected_not_treated_as_missing(self):
        """`data.get('review') or {}`のような書き方は、`review: false`のような
        falsyだが存在する不正値を「キー欠如」と誤認してしまう。それを防ぐ回帰
        テスト。"""
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": 1, "review": false}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_falsy_baseline_regressions_value_is_rejected_not_treated_as_missing(
        self,
    ):
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "done", "issue": 1, "baseline_regressions": 0}\n```\n'
        )
        comments = [{"body": body, "created_at": "x"}]
        assert parse_from_comments(comments) is None

    def test_invalid_comment_among_valid_ones_is_ignored(self):
        valid = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [
            {"body": "no marker here", "created_at": "2026-08-21T00:00:00Z"},
            {"body": valid.render(), "created_at": "2026-08-20T00:00:00Z"},
        ]
        assert parse_from_comments(comments) == valid


class TestParseFromCommentsSinceFilter:
    def test_ignores_comments_before_since_timestamp(self):
        # 2026-08-20T00:00:00Z is timestamp 1787184000.0
        # 2026-08-21T00:00:00Z is timestamp 1787270400.0
        # 2026-08-22T00:00:00Z is timestamp 1787356800.0
        stale_record = OutcomeRecord(result="not-needed", issue=1)
        fresh_record = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [
            {"body": stale_record.render(), "created_at": "2026-08-20T00:00:00Z"},
            {"body": fresh_record.render(), "created_at": "2026-08-22T00:00:00Z"},
        ]
        # since = 2026-08-21T00:00:00Z -> excludes stale_record
        since_ts = 1787270400.0
        assert parse_from_comments(comments, since=since_ts) == fresh_record

    def test_returns_none_if_all_comments_are_stale(self):
        stale_record = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [
            {"body": stale_record.render(), "created_at": "2026-08-20T00:00:00Z"}
        ]
        since_ts = 1787270400.0  # 2026-08-21T00:00:00Z
        assert parse_from_comments(comments, since=since_ts) is None

    def test_unparseable_timestamp_is_not_filtered(self):
        record = OutcomeRecord(result="done", issue=1, pr=2)
        comments = [{"body": record.render(), "created_at": "invalid-timestamp"}]
        since_ts = 1787270400.0
        assert parse_from_comments(comments, since=since_ts) == record


class TestParseFromCommentsTypeNormalization:
    def test_string_integer_issue_and_pr_normalized(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": "548", "pr": "560"}}\n```\n'
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        record = parse_from_comments(comments)
        assert record == OutcomeRecord(result="done", issue=548, pr=560)

    def test_hash_prefixed_issue_and_pr_normalized(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": "#548", "pr": "#560"}}\n```\n'
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        record = parse_from_comments(comments)
        assert record == OutcomeRecord(result="done", issue=548, pr=560)

    def test_whitespace_around_issue_and_pr_normalized(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": " #548 ", "pr": " 560 "}}\n```\n'
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        record = parse_from_comments(comments)
        assert record == OutcomeRecord(result="done", issue=548, pr=560)

    def test_string_attempt_and_rounds_normalized(self):
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "done", "issue": 548, "pr": 560, "attempt": "2", "review": {"rounds": "3", "verdict": "lgtm"}}\n```\n'
        )
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        record = parse_from_comments(comments)
        assert record == OutcomeRecord(
            result="done",
            issue=548,
            pr=560,
            attempt=2,
            review=ReviewSummary(rounds=3, verdict="lgtm"),
        )

    def test_invalid_string_integers_fail_closed(self):
        for invalid_issue in ("abc", "#abc", "12a", "", " ", "#", "-1", "12.34"):
            body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": "{invalid_issue}", "pr": 1}}\n```\n'
            comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
            assert (
                parse_from_comments(comments) is None
            ), f"Expected {invalid_issue} to be rejected"

    def test_float_issue_fails_closed(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": 12.34, "pr": 1}}\n```\n'
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) is None
