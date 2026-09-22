"""#548: `orchestune:outcome`完了宣言レコードのrender/parse往復変換テスト。"""

from __future__ import annotations

from orchestune.outcome_record import (
    OUTCOME_MARKER,
    OutcomeLookupResult,
    OutcomeLookupState,
    OutcomeRecord,
    ReviewSummary,
    calculate_blocked_attempt,
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

    def test_blocked_with_unknown_reason_preserves_original_reason(self):
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "blocked", "issue": 1, "reason": "flaky-network"}\n```\n'
        )
        comments = [{"body": body, "created_at": "x"}]
        record = parse_from_comments(comments)
        assert record is not None
        assert record.result == "blocked"
        assert record.reason == "flaky-network"

    def test_blocked_with_review_timeout_round_trips(self):
        record = OutcomeRecord(
            result="blocked",
            issue=1,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=1,
        )
        parsed = parse_from_comments([{"body": record.render(), "created_at": "x"}])
        assert parsed == record

    def test_non_blocked_with_unknown_reason_returns_none(self):
        body_done = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "done", "issue": 1, "pr": 2, "reason": "unknown-reason"}\n```\n'
        )
        assert parse_from_comments([{"body": body_done, "created_at": "x"}]) is None
        body_not_needed = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "not-needed", "issue": 1, "reason": "unknown-reason"}\n```\n'
        )
        assert (
            parse_from_comments([{"body": body_not_needed, "created_at": "x"}]) is None
        )

    def test_non_blocked_with_valid_reason_is_accepted(self):
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            '{"result": "not-needed", "issue": 1, "reason": "review-timeout"}\n```\n'
        )
        record = parse_from_comments([{"body": body, "created_at": "x"}])
        assert record is not None
        assert record.result == "not-needed"
        assert record.reason == "review-timeout"

    def test_is_known_reason(self):
        from orchestune.outcome_record import (
            REASON_BASE_BRANCH_RED,
            REASON_REVIEW_TIMEOUT,
            is_known_reason,
        )

        assert is_known_reason(REASON_BASE_BRANCH_RED) is True
        assert is_known_reason(REASON_REVIEW_TIMEOUT) is True
        assert is_known_reason("flaky-network") is False
        assert is_known_reason(None) is False

    def test_reason_sanitization_and_length_limit(self):
        long_reason = "a" * 150
        body = (
            f"{OUTCOME_MARKER}\n```json\n"
            f'{{"result": "blocked", "issue": 1, "reason": "bad\\n\\rcontrol\\t  {long_reason}"}}\n```\n'
        )
        comments = [{"body": body, "created_at": "x"}]
        record = parse_from_comments(comments)
        assert record is not None
        assert "\n" not in record.reason
        assert "\r" not in record.reason
        assert "\t" not in record.reason
        assert len(record.reason) <= 100

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


class TestIdentityFieldsRoundTrip:
    """#998: `claim_id`/`head_sha`/`completion_id`はIssue正本の同一作業試行識別子。"""

    def test_full_identity_fields_round_trip(self):
        record = OutcomeRecord(
            result="blocked",
            issue=548,
            reason="base-branch-red",
            attempt=2,
            claim_id="claim-abc123",
            head_sha="deadbeef",
            completion_id="completion-xyz",
        )
        body = record.render()
        comments = [{"body": body, "created_at": "2026-08-21T00:00:00Z"}]
        assert parse_from_comments(comments) == record

    def test_identity_fields_default_to_none_and_omitting_them_is_valid(self):
        body = f'{OUTCOME_MARKER}\n```json\n{{"result": "done", "issue": 1}}\n```\n'
        comments = [{"body": body, "created_at": "x"}]
        record = parse_from_comments(comments)
        assert record is not None
        assert record.claim_id is None
        assert record.head_sha is None
        assert record.completion_id is None

    def test_non_string_identity_fields_are_rejected(self):
        for field_name in ("claim_id", "head_sha", "completion_id"):
            body = (
                f"{OUTCOME_MARKER}\n```json\n"
                f'{{"result": "done", "issue": 1, "{field_name}": 123}}\n```\n'
            )
            comments = [{"body": body, "created_at": "x"}]
            assert (
                parse_from_comments(comments) is None
            ), f"Expected non-string {field_name} to be rejected"


class TestOutcomeLookupResult:
    def test_found_state_carries_the_record(self):
        record = OutcomeRecord(result="done", issue=1, pr=2)
        result = OutcomeLookupResult(state=OutcomeLookupState.FOUND, record=record)
        assert result.state is OutcomeLookupState.FOUND
        assert result.record == record

    def test_absent_and_unknown_states_carry_no_record(self):
        absent = OutcomeLookupResult(state=OutcomeLookupState.ABSENT)
        unknown = OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN)
        assert absent.record is None
        assert unknown.record is None
        assert absent.state is not unknown.state


class TestCalculateBlockedAttempt:
    """#998: Issueコメント履歴からbase-branch-redの連続attemptを算出する。
    旧PRコメント・旧形式（identityフィールド欠如）レコードは対象外。
    """

    def _blocked_comment(
        self,
        *,
        attempt: int,
        claim_id: str,
        head_sha: str,
        created_at: str,
        issue: int = 1,
    ) -> dict:
        record = OutcomeRecord(
            result="blocked",
            issue=issue,
            reason="base-branch-red",
            attempt=attempt,
            claim_id=claim_id,
            head_sha=head_sha,
        )
        return {"body": record.render(), "created_at": created_at}

    def test_first_attempt_with_no_history_is_one(self):
        assert (
            calculate_blocked_attempt(
                [], issue_number=1, claim_id="claim-a", head_sha="sha-a"
            )
            == 1
        )

    def test_new_claim_and_head_sha_increments_prior_attempt(self):
        comments = [
            self._blocked_comment(
                attempt=1,
                claim_id="claim-a",
                head_sha="sha-a",
                created_at="2026-08-20T00:00:00Z",
            )
        ]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-b", head_sha="sha-b"
            )
            == 2
        )

    def test_retransmission_of_same_claim_and_head_sha_does_not_increment(self):
        comments = [
            self._blocked_comment(
                attempt=2,
                claim_id="claim-a",
                head_sha="sha-a",
                created_at="2026-08-20T00:00:00Z",
            )
        ]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-a", head_sha="sha-a"
            )
            == 2
        )

    def test_legacy_record_without_identity_fields_is_excluded(self):
        legacy = OutcomeRecord(
            result="blocked", issue=1, reason="base-branch-red", attempt=5
        )
        comments = [{"body": legacy.render(), "created_at": "2026-08-20T00:00:00Z"}]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-a", head_sha="sha-a"
            )
            == 1
        )

    def test_records_for_other_issues_are_excluded(self):
        comments = [
            self._blocked_comment(
                attempt=7,
                claim_id="claim-a",
                head_sha="sha-a",
                created_at="2026-08-20T00:00:00Z",
                issue=999,
            )
        ]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-b", head_sha="sha-b"
            )
            == 1
        )

    def test_non_blocked_or_non_base_branch_red_records_are_excluded(self):
        done = OutcomeRecord(
            result="done", issue=1, claim_id="claim-a", head_sha="sha-a"
        )
        review_timeout = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="review-timeout",
            attempt=9,
            claim_id="claim-a",
            head_sha="sha-a",
        )
        comments = [
            {"body": done.render(), "created_at": "2026-08-20T00:00:00Z"},
            {"body": review_timeout.render(), "created_at": "2026-08-20T00:00:01Z"},
        ]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-b", head_sha="sha-b"
            )
            == 1
        )

    def test_uses_the_highest_qualifying_attempt_regardless_of_comment_order(self):
        comments = [
            self._blocked_comment(
                attempt=1,
                claim_id="claim-a",
                head_sha="sha-a",
                created_at="2026-08-20T00:00:00Z",
            ),
            self._blocked_comment(
                attempt=2,
                claim_id="claim-b",
                head_sha="sha-b",
                created_at="2026-08-21T00:00:00Z",
            ),
        ]
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-c", head_sha="sha-c"
            )
            == 3
        )
