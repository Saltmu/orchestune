"""Child review gate decision logic, digest calculation, and outcome lookup.

AutoMergeChildIntegrationStep executes this gate before updating parent branch
to ensure all children being merged have valid passing review evidence matching
the merged commit SHA (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
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
REASON_INTEGRATION_EVIDENCE_MISSING = "integration_evidence_missing"


@dataclass(frozen=True)
class ChildReviewGateInput:
    issue_number: int
    expected_commit_oid: str
    lookup_result: OutcomeLookupResult
    subtask_id: str | None = None


@dataclass(frozen=True)
class ChildReviewGateFailure:
    issue_number: int | None
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
            "subtask": f.subtask_id or "",
            "reason": f.reason,
            "sha": f.expected_sha or "",
        }
        for f in sorted(
            failures,
            key=lambda x: (
                x.issue_number if x.issue_number is not None else -1,
                x.subtask_id or "",
            ),
        )
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
    precomputed_failures: Sequence[ChildReviewGateFailure] = (),
) -> ChildReviewGateDecision:
    """各子のOutcomeLookupResultとマージ対象SHAを検証する純粋判定関数。"""
    failures: list[ChildReviewGateFailure] = list(precomputed_failures)
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
    if reason in (
        REASON_LEGACY,
        REASON_SKIPPED,
        REASON_NOT_PASS,
        REASON_SHA_MISMATCH,
    ):
        return (
            "完了がhandoff済みの場合、`orchestune complete` の再実行では証跡の追加・差し替えはできません。"
            "証跡なしで受け入れて進める実行に限り、明示的に `--child-review-gate off` を指定して再開してください"
            "（その実行のすべての子のレビュー検証が無効になります）。"
            "handoff前で `complete` が拒否され、何も投稿されていない場合は、"
            "現在のheadの再レビュー・判断表の補完などで原因を解消して `orchestune complete` を再実行してください。"
            "詳しい再開手順は usage §4.5（"
            "[日本語](https://github.com/Saltmu/orchestune/blob/main/docs/ja/usage.md#45-子レビュー証跡ゲート) / "
            "[English](https://github.com/Saltmu/orchestune/blob/main/docs/en/usage.md#45-child-review-evidence-gate)"
            "）を参照してください。"
        )
    if reason == REASON_ABSENT:
        return "子タスク完了時に `orchestune complete` を実行してOutcome Recordを投稿してください。"
    if reason == REASON_LOOKUP_UNKNOWN:
        return "API障害等の一時的な問題の可能性があるため、ディスパッチまたは統合を再実行してください。"
    if reason == REASON_INTEGRATION_EVIDENCE_MISSING:
        return "統合証跡と子の対応を復旧して再実行してください。"
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
    for failure in sorted(
        failures,
        key=lambda f: (
            f.issue_number if f.issue_number is not None else -1,
            f.subtask_id or "",
        ),
    ):
        issue_str = (
            f"#{failure.issue_number}" if failure.issue_number is not None else "—"
        )
        subtask = f"`{failure.subtask_id}`" if failure.subtask_id else "—"
        sha = f"`{failure.expected_sha[:7]}`" if failure.expected_sha else "—"
        instruction = _instruction_for_reason(failure.reason)
        lines.append(
            f"| {issue_str} | {subtask} | `{failure.reason}` | {sha} | {instruction} |"
        )

    lines.append("")
    lines.append(
        "> [!NOTE]\n"
        "> レビュー検証を意図的に省略して自動マージを進める場合は、"
        "`orchestune-dispatch --child-review-gate off` を指定して再実行してください"
        "（注: 本オプションは統合対象のすべての子Issueのレビュー検証を無効化します）。"
    )
    return "\n".join(lines)


def fetch_child_review_gate_outcome(
    forge: Forge, issue_number: int
) -> OutcomeLookupResult:
    """Forge経由で子Issueのコメントを取得し、OutcomeLookupResultを返す。"""
    try:
        comments = forge.list_comments(issue_number)
    except Exception as error:
        print(
            f"Warning: Failed to fetch comments for child issue #{issue_number}: {error}",
            file=sys.stderr,
        )
        return OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN, record=None)

    record = find_child_outcome_record(comments, issue_number)
    if record is None:
        return OutcomeLookupResult(state=OutcomeLookupState.ABSENT, record=None)
    return OutcomeLookupResult(state=OutcomeLookupState.FOUND, record=record)
