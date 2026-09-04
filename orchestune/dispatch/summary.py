"""#787: 1サイクルで「起動しなかったタスクと、その理由」の要約整形。

`SchedulingDecision`は選定フェーズに到達した候補にしか付かない。外部ロック・
重複PR・強制直列化・footprint逸脱で候補集合から落ちたタスクは判定そのものが
作られず、レポートにも現れなかった。`SkipRecord`はその欠落を埋める。

レンダラをテキストとMarkdownに分けているのは、出力先ごとに許される文字が
違うため。テキストはstderrへ出す想定で、`scripts/local-ci.ps1`を使うWindows
（コンソールがcp932）でも壊れないようASCIIだけで組む。絵文字を含む装飾は
GitHub Step SummaryとIssueコメント向けのMarkdown側だけで使う。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass

from orchestune.dispatch.scoring import (
    REASON_ALREADY_ACTIVE,
    REASON_BLOCKED_RECOMPUTE,
    REASON_CONFLICT,
    REASON_EXTERNAL_LOCK,
    REASON_LAUNCH_FAILED,
    REASON_QUOTA_EXHAUSTED,
    REASON_TOKEN_BUDGET,
    REASON_YAML_ERROR,
    SchedulingDecision,
)

#: 選定フェーズより前で候補から外れる理由。`scoring`が持つ理由コードとは
#: 名前空間を共有し、レポート上で同じ語彙として並べられるようにする。
REASON_DEPENDENCY = "dependency"
REASON_DUPLICATE_PR = "duplicate-pr"
REASON_FORCED_SERIAL = "forced-serial"
REASON_ACTOR_UNVERIFIED = "actor-unverified"
REASON_EARLY_DEATH_BACKOFF = "early-death-backoff"
REASON_DEVIATION_BLOCKED = "deviation-blocked"

SUMMARY_PREFIX = "[orchestune:summary]"
WARN_PREFIX = "[orchestune:warn]"

#: 集約表示する理由と、集約時に列挙するIssue番号の上限。依存待ちは規模の
#: 大きい親Issueでは数十件になり、全件並べるとサマリーが本題を埋めてしまう。
_AGGREGATED_REASONS = (REASON_DEPENDENCY,)
MAX_AGGREGATED_ISSUES = 3

_REASON_LABELS = {
    REASON_EXTERNAL_LOCK: "外部ロック中",
    REASON_DEPENDENCY: "依存タスク未完了",
    REASON_DUPLICATE_PR: "重複PR検出",
    REASON_FORCED_SERIAL: "強制直列化の待機中",
    REASON_ACTOR_UNVERIFIED: "ラベル付与者の権限が未確認",
    REASON_EARLY_DEATH_BACKOFF: "早期終了からの再投入待ち",
    REASON_DEVIATION_BLOCKED: "footprint逸脱によりブロック",
    REASON_QUOTA_EXHAUSTED: "クオータ枯渇",
    REASON_TOKEN_BUDGET: "トークン予算超過",
    REASON_CONFLICT: "競合するタスクが実行中",
    REASON_LAUNCH_FAILED: "起動に失敗",
    REASON_YAML_ERROR: "Footprint YAMLの解析に失敗",
    REASON_BLOCKED_RECOMPUTE: "依存グラフ再計算によりブロック",
    REASON_ALREADY_ACTIVE: "既に実行中",
}


@dataclass(frozen=True)
class SkipRecord:
    """起動されなかったタスク1件と、その理由。

    `detail`は理由を裏づける最小の手掛かり（衝突相手、待っている依存、既存PR
    など）。テキストレンダラがそのまま出すため、ASCIIで組む。
    """

    issue_number: int
    subtask_id: str
    reason: str
    detail: str = ""


def ascii_safe(text: str) -> str:
    """テキスト経路へ出す文字列を、必ずASCIIで表現できる形へ落とす。

    PR#789レビュー対応(Codex P2): ブランチ名・ファイルパス・例外メッセージは
    外部由来で、非ASCII文字を含みうる。cp932のコンソールへそのまま書くと
    `UnicodeEncodeError`が送出され、「保守的に保留する」はずの判定がサイクル
    自体の失敗に化ける。原文はUTF-8で書かれるMarkdown経路とJSONレポートに残す。
    """
    return text.encode("ascii", "backslashreplace").decode("ascii")


def skip_record_to_dict(record: SkipRecord) -> dict:
    return dataclasses.asdict(record)


def merge_skips(
    skips: Iterable[SkipRecord], decisions: Iterable[SchedulingDecision] = ()
) -> list[SkipRecord]:
    """事前フィルタの記録と、選定フェーズの未選出判定を1つの並びにまとめる。

    同じIssueが複数回現れた場合は先に来たものを採る。候補から外れた理由の方が
    具体的で、運用者が次に取る行動に直結するため。

    PR#789レビュー対応(Codex P2): `skips`内での重複も先勝ちにする。dict内包表記
    による後勝ちだと、外部ロックの衝突詳細を持つ記録が、後段のフィルタが付けた
    詳細なしの記録で上書きされ、理由が失われる。
    """
    merged: dict[int, SkipRecord] = {}
    for record in skips:
        merged.setdefault(record.issue_number, record)
    for decision in decisions:
        if decision.selected or decision.issue_number in merged:
            continue
        merged[decision.issue_number] = SkipRecord(
            issue_number=decision.issue_number,
            subtask_id=decision.subtask_id,
            reason=decision.reason,
        )
    return [merged[number] for number in sorted(merged)]


def _partition_aggregated(
    records: list[SkipRecord],
) -> tuple[list[SkipRecord], dict[str, list[SkipRecord]]]:
    listed = [r for r in records if r.reason not in _AGGREGATED_REASONS]
    aggregated: dict[str, list[SkipRecord]] = {}
    for record in records:
        if record.reason in _AGGREGATED_REASONS:
            aggregated.setdefault(record.reason, []).append(record)
    return listed, aggregated


def _aggregate_issue_list(records: list[SkipRecord]) -> str:
    shown = records[:MAX_AGGREGATED_ISSUES]
    numbers = ", ".join(f"#{record.issue_number}" for record in shown)
    remainder = len(records) - len(shown)
    return f"{numbers}, +{remainder}" if remainder else numbers


def render_skipped_text(records: list[SkipRecord]) -> list[str]:
    """stderr向けの要約。ASCIIのみで組む。"""
    if not records:
        return []
    listed, aggregated = _partition_aggregated(records)
    lines = [f"{SUMMARY_PREFIX} skipped tasks ({len(records)})"]
    for record in listed:
        detail = f"  {record.detail}" if record.detail else ""
        lines.append(
            ascii_safe(
                f"{SUMMARY_PREFIX}   #{record.issue_number} "
                f"{record.subtask_id or '-'}  {record.reason}{detail}"
            )
        )
    for reason, group in aggregated.items():
        lines.append(
            ascii_safe(
                f"{SUMMARY_PREFIX}   {reason}: {len(group)} task(s) "
                f"({_aggregate_issue_list(group)})"
            )
        )
    return lines


def render_skipped_markdown(records: list[SkipRecord]) -> list[str]:
    if not records:
        return []
    listed, aggregated = _partition_aggregated(records)
    lines = ["### ⏸️ 未選定タスク（Skipped）", ""]
    if listed:
        lines.extend(
            ["| Issue | サブタスクID | 理由 | 詳細 |", "| --- | --- | --- | --- |"]
        )
        lines.extend(
            f"| #{record.issue_number} | `{record.subtask_id or '-'}` | "
            f"{_REASON_LABELS.get(record.reason, record.reason)} | "
            f"{record.detail or '-'} |"
            for record in listed
        )
        lines.append("")
    for reason, group in aggregated.items():
        lines.append(
            f"- {_REASON_LABELS.get(reason, reason)}: {len(group)}件 "
            f"（{_aggregate_issue_list(group)}）"
        )
    if aggregated:
        lines.append("")
    return lines


def _warning_text(warning: dict) -> str:
    issue_number = warning.get("issue_number")
    subject = f"issue #{issue_number}" if issue_number else "the repository"
    return (
        f"forge API call '{warning.get('operation', 'unknown')}' failed for "
        f"{subject}: {warning.get('error', 'unknown error')}"
    )


def render_forge_warnings_text(warnings: list[dict]) -> list[str]:
    """stderr向けのForge障害要約。ASCIIのみで組む。"""
    return [
        ascii_safe(f"{WARN_PREFIX} {_warning_text(warning)}") for warning in warnings
    ]


def render_forge_warnings_markdown(warnings: list[dict]) -> list[str]:
    if not warnings:
        return []
    lines = [
        "### ⚠️ Forge API 障害による判定保留",
        "",
        "| Issue | 操作 | エラー |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {f'#{n}' if (n := warning.get('issue_number')) else '-'} | "
        f"`{warning.get('operation', 'unknown')}` | "
        f"{warning.get('error', 'unknown error')} |"
        for warning in warnings
    )
    lines.append("")
    return lines
