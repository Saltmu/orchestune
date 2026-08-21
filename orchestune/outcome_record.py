"""#548: ワーカーが作業終了時にPR/Issueコメントへ残す機械可読な完了宣言
（`orchestune:outcome`）のスキーマ定義とパーサ。以降のすべてのサブタスクが
この契約に依存するため、依存を持たないL0インフラ層に置く。

resultはdone/not-needed/blockedの3値。blockedはreasonを持つ
（初期実装ではbase-branch-redのみ）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

OUTCOME_MARKER = "<!-- orchestune:outcome -->"

RESULT_DONE = "done"
RESULT_NOT_NEEDED = "not-needed"
RESULT_BLOCKED = "blocked"
VALID_RESULTS = frozenset({RESULT_DONE, RESULT_NOT_NEEDED, RESULT_BLOCKED})

REASON_BASE_BRANCH_RED = "base-branch-red"
VALID_REASONS = frozenset({REASON_BASE_BRANCH_RED})


@dataclass(frozen=True)
class ReviewSummary:
    bot: str | None = None
    rounds: int | None = None
    verdict: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"bot": self.bot, "rounds": self.rounds, "verdict": self.verdict}


@dataclass(frozen=True)
class OutcomeRecord:
    result: str
    issue: int
    pr: int | None = None
    reason: str | None = None
    base_sha: str | None = None
    attempt: int | None = None
    review: ReviewSummary = field(default_factory=ReviewSummary)
    ci: str | None = None
    baseline_regressions: tuple[str, ...] = ()

    def render(self) -> str:
        """`parse_from_comments`で往復変換できるコメント本文を生成する。"""
        payload: dict[str, Any] = {
            "result": self.result,
            "issue": self.issue,
            "pr": self.pr,
            "reason": self.reason,
            "base_sha": self.base_sha,
            "attempt": self.attempt,
            "review": self.review.to_dict(),
            "ci": self.ci,
            "baseline_regressions": list(self.baseline_regressions),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        return f"{OUTCOME_MARKER}\n```json\n{body}\n```\n"


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _record_from_dict(data: Mapping[str, Any]) -> OutcomeRecord | None:
    result = data.get("result")
    if result not in VALID_RESULTS:
        return None

    issue = data.get("issue")
    if not _is_plain_int(issue):
        return None

    pr = data.get("pr")
    if pr is not None and not _is_plain_int(pr):
        return None

    reason = data.get("reason")
    if result == RESULT_BLOCKED:
        if reason not in VALID_REASONS:
            return None
    elif reason is not None and reason not in VALID_REASONS:
        return None

    base_sha = data.get("base_sha")
    if base_sha is not None and not isinstance(base_sha, str):
        return None

    attempt = data.get("attempt")
    if attempt is not None and not _is_plain_int(attempt):
        return None

    review_data = data.get("review") or {}
    if not isinstance(review_data, Mapping):
        return None
    review_bot = review_data.get("bot")
    if review_bot is not None and not isinstance(review_bot, str):
        return None
    review_rounds = review_data.get("rounds")
    if review_rounds is not None and not _is_plain_int(review_rounds):
        return None
    review_verdict = review_data.get("verdict")
    if review_verdict is not None and not isinstance(review_verdict, str):
        return None

    ci = data.get("ci")
    if ci is not None and not isinstance(ci, str):
        return None

    baseline_regressions = data.get("baseline_regressions") or []
    if not isinstance(baseline_regressions, list) or not all(
        isinstance(item, str) for item in baseline_regressions
    ):
        return None

    return OutcomeRecord(
        result=result,
        issue=cast(int, issue),
        pr=pr,
        reason=reason,
        base_sha=base_sha,
        attempt=attempt,
        review=ReviewSummary(
            bot=review_bot, rounds=review_rounds, verdict=review_verdict
        ),
        ci=ci,
        baseline_regressions=tuple(baseline_regressions),
    )


def _extract_record(body: str) -> OutcomeRecord | None:
    marker_pos = body.find(OUTCOME_MARKER)
    if marker_pos == -1:
        return None
    rest = body[marker_pos + len(OUTCOME_MARKER) :]
    fence_start = rest.find("```json")
    if fence_start == -1:
        return None
    fence_body_start = fence_start + len("```json")
    fence_end = rest.find("```", fence_body_start)
    if fence_end == -1:
        return None
    raw_json = rest[fence_body_start:fence_end].strip()
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    return _record_from_dict(data)


def parse_from_comments(
    comments: Sequence[Mapping[str, Any]],
) -> OutcomeRecord | None:
    """コメント列からoutcomeレコードを復元する。

    複数のoutcomeコメントが存在する場合は`created_at`が最大（最新）のものを
    採用する。マーカー不在・マーカー重複・不正JSON・スキーマ不一致のいずれの
    場合も例外を送出せず、該当コメントを無視するか全体としてNoneを返す。
    """
    latest_created_at: str | None = None
    latest_record: OutcomeRecord | None = None
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        record = _extract_record(body)
        if record is None:
            continue
        created_at = comment.get("created_at")
        created_at = created_at if isinstance(created_at, str) else ""
        if latest_created_at is None or created_at >= latest_created_at:
            latest_created_at = created_at
            latest_record = record
    return latest_record
