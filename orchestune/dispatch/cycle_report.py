"""1サイクル分の実行結果レポートと、KPI集計用イベントログの構築・追記。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestune.dispatch.scoring import SchedulingDecision, Task, decision_to_dict

if TYPE_CHECKING:
    from orchestune.dispatch.execution_profiles import ExecutionSelection


@dataclass
class CycleReport:
    selected: list[Task]
    quota_slots_available: int
    lock_changes: dict[str, list[Task]]
    deviation_events: list[dict]
    completion_events: list[dict]
    promotion_events: list[dict]
    applied: bool
    # #660: 全候補分の選定理由・rank・推定cost。既定値を持つのは、
    # スケジューリング以外の関心事でCycleReportを組み立てる呼び出し側
    # （レポート整形テスト等）に無関係な引数を強いないため。
    scheduling_decisions: list[SchedulingDecision] = field(default_factory=list)
    execution_selections: dict[int, ExecutionSelection] = field(default_factory=dict)


def build_event_log_entry(report: CycleReport, now: float) -> dict:
    """#239: KPI A1〜A4/C2/C3集計用に、1サイクル分のイベントをJSON Lines化する。"""
    selected_entries = []
    for t in report.selected:
        entry: dict[str, Any] = {
            "issue_number": t.issue_number,
            "subtask_id": t.subtask_id,
        }
        sel = report.execution_selections.get(t.issue_number)
        if sel is not None:
            entry["execution_profile"] = sel.profile
            if sel.model is not None:
                entry["model"] = sel.model
            if sel.reasoning_effort is not None:
                entry["reasoning_effort"] = sel.reasoning_effort
        elif t.execution_profile:
            entry["execution_profile"] = t.execution_profile
        selected_entries.append(entry)

    return {
        "timestamp": now,
        "quota_slots_available": report.quota_slots_available,
        "selected": selected_entries,
        "deviation_events": report.deviation_events,
        "completion_events": report.completion_events,
        "promotion_events": report.promotion_events,
        "scheduling_decisions": [
            decision_to_dict(decision) for decision in report.scheduling_decisions
        ],
    }


def append_event_log(entry: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
