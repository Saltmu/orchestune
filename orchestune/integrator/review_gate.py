"""Child review gate decision logic, digest calculation, and outcome lookup.

AutoMergeChildIntegrationStep executes this gate before updating parent branch
to ensure all children being merged have valid passing review evidence matching
the merged commit SHA (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestune.forge import Forge
from orchestune.outcome_record import (
    RESULT_DONE,
    OutcomeLookupResult,
    OutcomeLookupState,
    OutcomeRecord,
    find_child_outcome_record,
)

CHILD_REVIEW_GATE_MARKER_PREFIX = "<!-- orchestune:child-review-gate"
_MARKER_REGEX = re.compile(
    r"<!--\s*orchestune:child-review-gate\s+digest=([0-9a-fA-F]+)\s*-->"
)

REASON_LEGACY = "legacy"
REASON_SKIPPED = "skipped"
REASON_NOT_PASS = "not_pass"
REASON_SHA_MISMATCH = "sha_mismatch"
REASON_ABSENT = "absent"
REASON_LOOKUP_UNKNOWN = "lookup_unknown"


@dataclass(frozen=True)
class ChildReviewGateInput:
    issue_number: int
    expected_commit_oid: str
    lookup_result: OutcomeLookupResult
    subtask_id: str | None = None


@dataclass(frozen=True)
class ChildReviewGateFailure:
    issue_number: int
    reason: str
    subtask_id: str | None = None
    expected_sha: str | None = None
    actual_sha: str | None = None
    actual_verdict: str | None = None


@dataclass(frozen=True)
class ChildReviewGateDecision:
    passed: bool
    failures: tuple[ChildReviewGateFailure, ...] = ()
    digest: str = ""


def compute_review_gate_digest(failures: Sequence[ChildReviewGateFailure]) -> str:
    """子番号・理由・SHAの正規化集合からsha256ハッシュを計算する。"""
    normalized = [
        {
            "issue": f.issue_number,
            "reason": f.reason,
            "sha": f.expected_sha or "",
        }
        for f in sorted(failures, key=lambda x: x.issue_number)
    ]
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_child_review_gate_digest(comment_body: str) -> str | None:
    """コメント本文から child-review-gate digest を抽出する。"""
    match = _MARKER_REGEX.search(comment_body)
    if match:
        return match.group(1).lower()
    return None


def has_matching_review_gate_comment(
    comments: Sequence[Mapping[str, Any]], digest: str
) -> bool:
    """コメント一覧に同一digestのchild-review-gateマーカーが存在するか判定する。"""
    target = digest.lower()
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str) and parse_child_review_gate_digest(body) == target:
            return True
    return False


def _evaluate_outcome_record(
    item: ChildReviewGateInput, record: OutcomeRecord
) -> ChildReviewGateFailure | None:
    verdict = record.review.verdict
    actual_head_sha = record.head_sha
    reviewed_head_sha = record.review.reviewed_head_sha

    if verdict is None:
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_LEGACY,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
            actual_sha=actual_head_sha,
            actual_verdict=verdict,
        )
    if verdict == "skipped":
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_SKIPPED,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
            actual_sha=actual_head_sha,
            actual_verdict=verdict,
        )
    if verdict != "pass" or record.result != RESULT_DONE:
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_NOT_PASS,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
            actual_sha=actual_head_sha,
            actual_verdict=verdict,
        )
    if (
        actual_head_sha != item.expected_commit_oid
        or reviewed_head_sha != item.expected_commit_oid
    ):
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_SHA_MISMATCH,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
            actual_sha=actual_head_sha or reviewed_head_sha,
            actual_verdict=verdict,
        )
    return None


def _evaluate_single_child(
    item: ChildReviewGateInput,
) -> ChildReviewGateFailure | None:
    lookup = item.lookup_result
    if lookup.state == OutcomeLookupState.UNKNOWN:
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_LOOKUP_UNKNOWN,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
        )
    if lookup.state == OutcomeLookupState.ABSENT or lookup.record is None:
        return ChildReviewGateFailure(
            issue_number=item.issue_number,
            reason=REASON_ABSENT,
            subtask_id=item.subtask_id,
            expected_sha=item.expected_commit_oid,
        )
    return _evaluate_outcome_record(item, lookup.record)


def decide_child_review_gate(
    inputs: Sequence[ChildReviewGateInput],
) -> ChildReviewGateDecision:
    """各子のOutcomeLookupResultとマージ対象SHAを検証する純粋判定関数。"""
    failures: list[ChildReviewGateFailure] = []
    for item in inputs:
        failure = _evaluate_single_child(item)
        if failure is not None:
            failures.append(failure)

    if not failures:
        return ChildReviewGateDecision(passed=True, failures=(), digest="")

    failures_tuple = tuple(failures)
    digest = compute_review_gate_digest(failures_tuple)
    return ChildReviewGateDecision(passed=False, failures=failures_tuple, digest=digest)


def _instruction_for_reason(reason: str) -> str:
    if reason in (REASON_LEGACY, REASON_SKIPPED):
        return (
            "子PRでコードレビューを実施して合格（pass）とし `orchestune complete` を再実行するか、"
            "`--child-review-gate off` でゲートを無効化してください。"
        )
    if reason == REASON_SHA_MISMATCH:
        return "子ブランチの更新後に再レビューを実施し、`orchestune complete` を再実行してください。"
    if reason == REASON_NOT_PASS:
        return "子PRでレビュー指摘に対応して合格（pass）とし、`orchestune complete` を再実行してください。"
    if reason == REASON_ABSENT:
        return "子タスク完了時に `orchestune complete` を実行してOutcome Recordを投稿してください。"
    if reason == REASON_LOOKUP_UNKNOWN:
        return "API障害等の一時的な問題の可能性があるため、ディスパッチまたは統合を再実行してください。"
    return "子タスクの状態を確認の上、再実行してください。"


def format_child_review_gate_escalation_comment(
    failures: Sequence[ChildReviewGateFailure], digest: str
) -> str:
    """親Issue用のエスカレーションコメント本文を生成する。"""
    marker = f"{CHILD_REVIEW_GATE_MARKER_PREFIX} digest={digest} -->"
    lines = [
        marker,
        "## ⚠️ 子タスクレビューゲートによる統合停止 (Child Review Gate Blocked)",
        "",
        "親ブランチへの自動マージ前に実施された各子タスクのレビュー合格証跡の検証において、"
        "基準を満たさない子Issueが検出されたため、親ブランチの更新を停止し "
        "`status:blocked-human-review` へエスカレーションしました。",
        "",
        "| 子Issue | サブタスクID | 判定結果 | 対象SHA | 再開方法 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for failure in sorted(failures, key=lambda f: f.issue_number):
        subtask = f"`{failure.subtask_id}`" if failure.subtask_id else "—"
        sha = f"`{failure.expected_sha[:7]}`" if failure.expected_sha else "—"
        instruction = _instruction_for_reason(failure.reason)
        lines.append(
            f"| #{failure.issue_number} | {subtask} | `{failure.reason}` | {sha} | {instruction} |"
        )

    lines.append("")
    lines.append(
        "> [!NOTE]\n"
        "> レビュー検証を意図的に省略して自動マージを進める場合は、"
        "`orchestune-dispatch --child-review-gate off` を指定して再実行してください。"
    )
    return "\n".join(lines)


def fetch_child_review_gate_outcome(
    forge: Forge, issue_number: int
) -> OutcomeLookupResult:
    """Forge経由で子Issueのコメントを取得し、OutcomeLookupResultを返す。"""
    try:
        comments = forge.list_comments(issue_number)
    except Exception:
        return OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN, record=None)

    record = find_child_outcome_record(comments, issue_number)
    if record is None:
        return OutcomeLookupResult(state=OutcomeLookupState.ABSENT, record=None)
    return OutcomeLookupResult(state=OutcomeLookupState.FOUND, record=record)
