"""#548: ワーカーが作業終了時にPR/Issueコメントへ残す機械可読な完了宣言
（`orchestune:outcome`）のスキーマ定義とパーサ。以降のすべてのサブタスクが
この契約に依存するため、依存を持たないL0インフラ層に置く。

resultはdone/not-needed/blockedの3値。blockedはreasonを持つ
（初期実装ではbase-branch-redのみ）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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


def _normalize_int(value: Any) -> int | None:
    """Normalize a value to an integer if possible, rejecting booleans, floats, and non-digit strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("#"):
            s = s[1:].strip()
        if s.isdigit():
            try:
                return int(s)
            except ValueError:
                return None
    return None


_INVALID = object()


def _review_from_value(value: Any) -> ReviewSummary | object:
    """`value`が`None`（キー欠如相当）ならデフォルトの`ReviewSummary`を、有効な
    マッピングなら検証済みの`ReviewSummary`を返す。それ以外は`_INVALID`を返す
    （falsyだが存在する不正値をキー欠如と誤認しないよう、`or {}`は使わない）。"""
    if value is None:
        value = {}
    elif not isinstance(value, Mapping):
        return _INVALID
    bot = value.get("bot")
    if bot is not None and not isinstance(bot, str):
        return _INVALID
    rounds_raw = value.get("rounds")
    rounds: int | None = None
    if rounds_raw is not None:
        rounds = _normalize_int(rounds_raw)
        if rounds is None:
            return _INVALID
    verdict = value.get("verdict")
    if verdict is not None and not isinstance(verdict, str):
        return _INVALID
    return ReviewSummary(bot=bot, rounds=rounds, verdict=verdict)


def _baseline_regressions_from_value(value: Any) -> tuple[str, ...] | object:
    """`value`が`None`（キー欠如相当）なら空タプルを、有効な文字列リストなら
    タプル化したものを返す。それ以外は`_INVALID`を返す（`_review_from_value`と
    同じくfalsy-but-present値を誤って許容しないため）。"""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _INVALID
    return tuple(value)


def _record_from_dict(data: Mapping[str, Any]) -> OutcomeRecord | None:
    result = data.get("result")
    if result not in VALID_RESULTS:
        return None

    issue = _normalize_int(data.get("issue"))
    if issue is None:
        return None

    pr_raw = data.get("pr")
    pr: int | None = None
    if pr_raw is not None:
        pr = _normalize_int(pr_raw)
        if pr is None:
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

    attempt_raw = data.get("attempt")
    attempt: int | None = None
    if attempt_raw is not None:
        attempt = _normalize_int(attempt_raw)
        if attempt is None:
            return None

    review = _review_from_value(data.get("review"))
    if review is _INVALID:
        return None

    ci = data.get("ci")
    if ci is not None and not isinstance(ci, str):
        return None

    baseline_regressions = _baseline_regressions_from_value(
        data.get("baseline_regressions")
    )
    if baseline_regressions is _INVALID:
        return None

    return OutcomeRecord(
        result=result,
        issue=issue,
        pr=pr,
        reason=reason,
        base_sha=base_sha,
        attempt=attempt,
        review=cast(ReviewSummary, review),
        ci=ci,
        baseline_regressions=cast("tuple[str, ...]", baseline_regressions),
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


def _parse_comment_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def parse_from_comments(
    comments: Sequence[Mapping[str, Any]],
    since: float | None = None,
) -> OutcomeRecord | None:
    """コメント列からoutcomeレコードを復元する。

    複数のoutcomeコメントが存在する場合は`created_at`が最大（最新）のものを
    採用する。`since`が指定された場合、`since`（UNIXエポック秒）より前に投稿された
    古いコメントは除外する。マーカー不在・マーカー重複・不正JSON・スキーマ不一致の
    いずれの場合も例外を送出せず、該当コメントを無視するか全体としてNoneを返す。
    """
    latest_created_at: str | None = None
    latest_record: OutcomeRecord | None = None
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        created_at_raw = comment.get("created_at") or comment.get("createdAt")
        created_at = created_at_raw if isinstance(created_at_raw, str) else ""
        if since is not None and created_at:
            ts = _parse_comment_timestamp(created_at)
            if ts is not None and ts < math.floor(since):
                continue

        record = _extract_record(body)
        if record is None:
            continue
        if latest_created_at is None or created_at >= latest_created_at:
            latest_created_at = created_at
            latest_record = record
    return latest_record
