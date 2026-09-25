"""#998: Issue正本のOutcomeRecord識別子と履歴選択の統合シナリオ。

`tests/test_outcome_record.py`が`calculate_blocked_attempt`/`OutcomeLookupResult`の
単体契約を検証するのに対し、本ファイルは実際のIssueコメント欄で起こりうる
現実的な時系列（旧形式レコードの混在、再送、リベース後の再試行、最終的な
完了）を通して、`parse_from_comments`（最新レコード選択）と
`calculate_blocked_attempt`（attempt履歴選択）が組み合わさって正しく動作する
ことを検証する。
"""

from __future__ import annotations

from orchestune.outcome_record import (
    OutcomeRecord,
    calculate_blocked_attempt,
    parse_from_comments,
)


def _comment(record: OutcomeRecord, created_at: str) -> dict:
    return {"body": record.render(), "created_at": created_at}


class TestHistorySelectionWithLegacyRecordsMixedIn:
    """#998: 旧PRコメント時代・#998より前のレコード（identityフィールド欠如）は
    attempt計算からは除外されるが、`parse_from_comments`の「最新レコード」選択
    そのものは（識別子の有無を問わず）従来どおり成立する。"""

    def test_legacy_blocked_record_does_not_inflate_new_attempt_count(self):
        legacy = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="aaa",
            attempt=2,
        )
        comments = [_comment(legacy, "2026-08-01T00:00:00Z")]
        # 旧形式（claim_id/head_sha欠如）はattempt計算の対象外なので、
        # 新形式での最初の試行はattempt=1から始まる。
        assert (
            calculate_blocked_attempt(
                comments, issue_number=1, claim_id="claim-1", head_sha="sha-1"
            )
            == 1
        )

    def test_latest_record_selection_still_picks_the_newest_regardless_of_format(
        self,
    ):
        legacy = OutcomeRecord(result="done", issue=1, pr=10)
        new_format = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            attempt=1,
            claim_id="claim-1",
            head_sha="sha-1",
        )
        comments = [
            _comment(legacy, "2026-08-01T00:00:00Z"),
            _comment(new_format, "2026-08-02T00:00:00Z"),
        ]
        assert parse_from_comments(comments) == new_format


class TestBlockedRetryLifecycle:
    """base-branch-redで複数回blockedになり、最終的にdoneへ至る現実的な
    Issueコメント欄の時系列を通した履歴選択。"""

    def test_full_lifecycle_from_first_attempt_through_retransmission_to_new_attempt(
        self,
    ):
        history: list[dict] = []

        # 1回目のblocked投稿（claim-a, head sha-a）。
        attempt_1 = calculate_blocked_attempt(
            history, issue_number=1, claim_id="claim-a", head_sha="sha-a"
        )
        assert attempt_1 == 1
        record_1 = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="base-1",
            attempt=attempt_1,
            claim_id="claim-a",
            head_sha="sha-a",
        )
        history.append(_comment(record_1, "2026-08-01T00:00:00Z"))

        # ネットワーク再送: 同じclaim/head_shaで再投稿されても増加しない。
        retransmit_attempt = calculate_blocked_attempt(
            history, issue_number=1, claim_id="claim-a", head_sha="sha-a"
        )
        assert retransmit_attempt == 1

        # ベースブランチ前進後、同一claimがリベースして再試行（head_sha変化）。
        attempt_2 = calculate_blocked_attempt(
            history, issue_number=1, claim_id="claim-a", head_sha="sha-a-rebased"
        )
        assert attempt_2 == 2
        record_2 = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="base-2",
            attempt=attempt_2,
            claim_id="claim-a",
            head_sha="sha-a-rebased",
        )
        history.append(_comment(record_2, "2026-08-02T00:00:00Z"))

        # 3回目: 新しいclaim（reclaim後）での試行。
        attempt_3 = calculate_blocked_attempt(
            history, issue_number=1, claim_id="claim-b", head_sha="sha-b"
        )
        assert attempt_3 == 3
        record_3 = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="base-3",
            attempt=attempt_3,
            claim_id="claim-b",
            head_sha="sha-b",
        )
        history.append(_comment(record_3, "2026-08-03T00:00:00Z"))

        # エスカレーション閾値（3）に達している。
        assert attempt_3 >= 3

        # 最終的にdoneが投稿されれば、最新レコード選択はdoneを返す。
        done = OutcomeRecord(
            result="done",
            issue=1,
            pr=42,
            claim_id="claim-b",
            head_sha="sha-b",
            completion_id="completion-1",
        )
        history.append(_comment(done, "2026-08-04T00:00:00Z"))
        assert parse_from_comments(history) == done

    def test_other_issues_and_other_reasons_never_pollute_this_issues_attempt_count(
        self,
    ):
        history = [
            _comment(
                OutcomeRecord(
                    result="blocked",
                    issue=2,  # 別Issue
                    reason="base-branch-red",
                    attempt=9,
                    claim_id="claim-x",
                    head_sha="sha-x",
                ),
                "2026-08-01T00:00:00Z",
            ),
            _comment(
                OutcomeRecord(
                    result="blocked",
                    issue=1,
                    reason="review-timeout",  # 別のreason（別カウンタで管理）
                    attempt=9,
                    claim_id="claim-y",
                    head_sha="sha-y",
                ),
                "2026-08-01T00:00:01Z",
            ),
        ]
        assert (
            calculate_blocked_attempt(
                history, issue_number=1, claim_id="claim-z", head_sha="sha-z"
            )
            == 1
        )
