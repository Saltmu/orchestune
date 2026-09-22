"""#548: ワーカーが作業終了時にPR/Issueコメントへ残す機械可読な完了宣言
（`orchestune:outcome`）のスキーマ定義とパーサ。以降のすべてのサブタスクが
この契約に依存するため、依存を持たないL0インフラ層に置く。

resultはdone/not-needed/blockedの3値。blockedはreasonを持つ
（初期実装ではbase-branch-redのみ）。
"""

from __future__ import annotations

import enum
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
REASON_REVIEW_TIMEOUT = "review-timeout"
VALID_REASONS = frozenset({REASON_BASE_BRANCH_RED, REASON_REVIEW_TIMEOUT})

MAX_REASON_LENGTH = 100


def is_known_reason(reason: str | None) -> bool:
    """Return whether the given reason is a known, schema-defined reason."""
    return reason in VALID_REASONS


def _sanitize_reason(reason: str) -> str:
    """Replace control characters with space, strip leading/trailing whitespace, and cap length."""
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in reason)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_REASON_LENGTH]


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
    # #998: Issue正本の同一作業試行を識別する任意フィールド。旧形式（PR
    # コメント時代）のレコードはこれらを持たないため、いずれも省略可能とし、
    # 既存の呼び出し元（GCなど、本Issueのスコープ外）の構築を壊さない。
    claim_id: str | None = None
    head_sha: str | None = None
    completion_id: str | None = None

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
            "claim_id": self.claim_id,
            "head_sha": self.head_sha,
            "completion_id": self.completion_id,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        return f"{OUTCOME_MARKER}\n```json\n{body}\n```\n"


class OutcomeLookupState(enum.Enum):
    """#998: Issueコメント取得結果を表す共有契約。

    `ABSENT`は全ページの取得が完了した上でレコードが見つからなかった場合
    にのみ返してよい。取得が途中で失敗した場合は`UNKNOWN`とし、両者を
    呼び出し元が区別できるようにする（fail-closed）。
    """

    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OutcomeLookupResult:
    """`OutcomeLookupState`と、`FOUND`時のみ設定される`OutcomeRecord`の組。"""

    state: OutcomeLookupState
    record: OutcomeRecord | None = None


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


def _extract_optional_int(raw: Any) -> int | None | object:
    if raw is None:
        return None
    val = _normalize_int(raw)
    return val if val is not None else _INVALID


def _extract_optional_str(raw: Any) -> str | None | object:
    if raw is None:
        return None
    return raw if isinstance(raw, str) else _INVALID


def _extract_reason(raw: Any, result: str) -> str | None | object:
    if raw is None:
        return None if result != RESULT_BLOCKED else _INVALID
    if not isinstance(raw, str):
        return _INVALID
    if result != RESULT_BLOCKED:
        return raw if raw in VALID_REASONS else _INVALID
    sanitized = _sanitize_reason(raw)
    return sanitized if sanitized else _INVALID


def _identity_fields_from_dict(
    data: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None] | object:
    """#998: `claim_id`/`head_sha`/`completion_id`（同一作業試行の識別子）を
    まとめて検証する。いずれか一つでも不正なら`_INVALID`を返す。"""
    claim_id = _extract_optional_str(data.get("claim_id"))
    if claim_id is _INVALID:
        return _INVALID
    head_sha = _extract_optional_str(data.get("head_sha"))
    if head_sha is _INVALID:
        return _INVALID
    completion_id = _extract_optional_str(data.get("completion_id"))
    if completion_id is _INVALID:
        return _INVALID
    return (
        cast("str | None", claim_id),
        cast("str | None", head_sha),
        cast("str | None", completion_id),
    )


def _record_from_dict(data: Mapping[str, Any]) -> OutcomeRecord | None:
    result = data.get("result")
    if result not in VALID_RESULTS:
        return None

    issue = _normalize_int(data.get("issue"))
    if issue is None:
        return None

    pr = _extract_optional_int(data.get("pr"))
    if pr is _INVALID:
        return None

    reason = _extract_reason(data.get("reason"), result)
    if reason is _INVALID:
        return None

    base_sha = _extract_optional_str(data.get("base_sha"))
    if base_sha is _INVALID:
        return None

    attempt = _extract_optional_int(data.get("attempt"))
    if attempt is _INVALID:
        return None

    review = _review_from_value(data.get("review"))
    if review is _INVALID:
        return None

    ci = _extract_optional_str(data.get("ci"))
    if ci is _INVALID:
        return None

    baseline_regressions = _baseline_regressions_from_value(
        data.get("baseline_regressions")
    )
    if baseline_regressions is _INVALID:
        return None

    identity = _identity_fields_from_dict(data)
    if identity is _INVALID:
        return None
    claim_id, head_sha, completion_id = cast("tuple[str | None, ...]", identity)

    return OutcomeRecord(
        result=result,
        issue=issue,
        pr=cast(int | None, pr),
        reason=cast(str | None, reason),
        base_sha=cast(str | None, base_sha),
        attempt=cast(int | None, attempt),
        review=cast(ReviewSummary, review),
        ci=cast(str | None, ci),
        baseline_regressions=cast("tuple[str, ...]", baseline_regressions),
        claim_id=claim_id,
        head_sha=head_sha,
        completion_id=completion_id,
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


def calculate_blocked_attempt(
    comments: Sequence[Mapping[str, Any]],
    *,
    issue_number: int,
    claim_id: str,
    head_sha: str,
) -> int:
    """Issueコメント履歴から、この`(claim_id, head_sha)`が投稿すべき
    base-branch-redのblocked attempt番号を算出する。

    対象は`result=blocked`・`reason=base-branch-red`・`issue`がこのIssue番号に
    一致し、かつ`claim_id`/`head_sha`の両方を持つレコードに限る（PRコメント
    時代・#998より前の旧形式レコードは識別子を持たないため対象外＝
    「旧形式レコードを...attempt計算から除外する」）。

    最新の対象レコードが呼び出し元と同じ`(claim_id, head_sha)`であれば、
    同一作業試行の再送（通信再送等）とみなしその`attempt`をそのまま返す
    （増加させない）。異なる場合は対象レコード中の最大`attempt`+1を返す
    （対象レコードが無ければ1）。
    """
    qualifying: list[OutcomeRecord] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        record = _extract_record(body)
        if record is None:
            continue
        if (
            record.result != RESULT_BLOCKED
            or record.reason != REASON_BASE_BRANCH_RED
            or record.issue != issue_number
            or record.claim_id is None
            or record.head_sha is None
        ):
            continue
        qualifying.append(record)

    if not qualifying:
        return 1

    for record in qualifying:
        if record.claim_id == claim_id and record.head_sha == head_sha:
            return record.attempt if record.attempt is not None else 1

    max_attempt = max(
        (r.attempt for r in qualifying if r.attempt is not None), default=0
    )
    return max_attempt + 1
