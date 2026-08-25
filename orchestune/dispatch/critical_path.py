"""Precedence DAG由来のスケジューリングrank（bottom level／後続解放数）の計算。

#660: 従来のディスパッチ選出はbase priority・待ち時間・partial progressしか見て
いなかったため、「短時間で多くの後続を解放する共有契約タスク」よりも、下流への
影響が小さいタスクが先に選ばれることがあった。ここではPrecedence DAG
（`Task.depends_on`）から、価値関数が使う次の3つのrankを決定論的に求める。

- **bottom level**: そのタスク自身の推定所要時間に、後続チェーンのうち最長のものを
  足した値。古典的なlist schedulingのbottom level（＝critical-path rank）であり、
  「このタスクを遅らせると全体完了がどれだけ遅れるか」の下限を表す。
- **unlocked**: 直接の後続数（このタスクが完了した瞬間にready集合へ入り得る数）。
- **downstream**: 到達可能な後続数（このタスクが塞いでいる下流全体の広さ）。

計算量と探索上限:

- bottom levelとunlockedは辺を一度ずつ辿るだけなのでO(V + E)。非循環である限り、
  ノード数に関わらず常に厳密に計算する（探索上限の対象外）。
- downstreamは推移閉包なので最悪O(V * E)。候補集合が大きい場合に暴走しないよう、
  ノード数が`MAX_TRANSITIVE_CLOSURE_NODES`を超えたら推移閉包を打ち切り、直接の
  後続数へ決定論的に縮退する（`PrecedenceRanks.exact_downstream`で観測可能）。

`depends_on`に循環がある場合（Issue本文の手編集などで起こり得る）、逆トポロジカル
順序が存在しないため1回の走査ではrankを正しく積み上げられず、bottom levelも
downstreamも過小評価になる。この場合も探索上限超過と同じく`exact_downstream`を
`False`にし、downstreamは直接の後続数へ縮退させる。壊れたメタデータのために
正確な到達可能性計算を持ち込むより、正直に縮退したことを通知する方が良い
（PR#665レビュー指摘）。

Conflict Graphはここでは扱わない。#659で分離したとおり、競合は対称な排他制約で
あって因果順序ではなく、rankの計算根拠にしてはならないため。
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from orchestune.models import Task

# 推定所要時間が渡されなかったノードの既定値。1.0にすることで、履歴が無い
# （＝全ノードが既定値になる）状況ではbottom levelがそのまま「残りチェーン長」
# という解釈しやすい値になる。
DEFAULT_UNIT_DURATION = 1.0

# 推移閉包を厳密に計算する上限ノード数（超過時はO(V+E)のヒューリスティックへ縮退）。
MAX_TRANSITIVE_CLOSURE_NODES = 512

# 「もう起動対象ではない」ことをラベルから判定するための集合。完了済みの後続を
# rankへ含めると、既に解放済みのチェーンの分だけbottom levelが過大評価される。
_FINISHED_STATUS_LABELS = frozenset({"status:done", "status:not-needed"})


def pending_tasks(tasks: Iterable[Task]) -> list[Task]:
    """rank計算の対象となる「まだ残っている」タスクだけを入力順に返す。"""
    return [
        task
        for task in tasks
        if task.subtask_id
        and task.issue_state == "OPEN"
        and not _FINISHED_STATUS_LABELS.intersection(task.status_labels)
    ]


@dataclass(frozen=True)
class PrecedenceRanks:
    """Precedence DAGから求めたrank群。未知のsubtask_idは0として扱う。"""

    bottom_level: Mapping[str, float]
    unlocked: Mapping[str, int]
    downstream: Mapping[str, int]
    # rankを厳密に求められたかどうか。`False`になるのは、探索上限
    # （`MAX_TRANSITIVE_CLOSURE_NODES`）を超えたときと、`depends_on`に循環が
    # あったとき。循環時は逆トポロジカル順序が存在せず、1回の走査では
    # `bottom_level`も`downstream`も過小評価になるため、`downstream`は直接の
    # 後続数へ縮退させたうえでこのフラグで通知する（PR#665レビュー指摘）。
    exact_downstream: bool = True

    def bottom_level_of(self, subtask_id: str) -> float:
        return self.bottom_level.get(subtask_id, 0.0)

    def unlocked_count(self, subtask_id: str) -> int:
        return self.unlocked.get(subtask_id, 0)

    def downstream_count(self, subtask_id: str) -> int:
        return self.downstream.get(subtask_id, 0)


def _successor_map(tasks: list[Task]) -> tuple[list[str], dict[str, list[str]]]:
    """`depends_on`を後続方向の隣接リストへ反転する。

    ディスパッチャーが見るのはIssue一覧のスナップショットであり、既に完了して
    一覧から消えた依存先や、手編集で壊れた自己参照が混じり得る。ここでは既知の
    ノードを指す辺だけを採用し、未知の依存先と自己参照は捨てる。
    """
    node_ids = sorted({task.subtask_id for task in tasks if task.subtask_id})
    known = set(node_ids)
    successors: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for task in tasks:
        if not task.subtask_id:
            continue
        for dependency in task.depends_on:
            if dependency in known and dependency != task.subtask_id:
                successors[dependency].add(task.subtask_id)
    return node_ids, {node: sorted(targets) for node, targets in successors.items()}


def _topological_order(
    node_ids: list[str], successors: Mapping[str, list[str]]
) -> tuple[list[str], bool]:
    """辞書順で正規化したトポロジカル順序と、循環を検出したかどうかを返す。

    Precedence DAGは`orchestune-dag`が起票前に循環検査済みだが、ディスパッチャー
    が読むのはIssue本文というユーザーが編集できる経路であり、循環が入り得る。
    ここで例外を投げるとサイクル全体が落ちてしまうため、閉路に残ったノードは
    辞書順で末尾へ回し、rank計算では0寄与として決定論的に縮退させる。

    PR#665レビュー指摘(Codex P2): その縮退はrankを**過小評価**するため、循環の
    有無を呼び出し側へ返す必要がある。返された順序は閉路部分では逆トポロジカル
    順序になっておらず、1回の逆順走査ではrankを正しく積み上げられない。
    """
    indegree = dict.fromkeys(node_ids, 0)
    for targets in successors.values():
        for target in targets:
            indegree[target] += 1

    ready = [node for node in node_ids if indegree[node] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for target in successors[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)

    placed = set(order)
    remaining = [node for node in node_ids if node not in placed]
    return order + remaining, bool(remaining)


def _duration_of(durations: Mapping[str, float], node: str) -> float:
    duration = durations.get(node, DEFAULT_UNIT_DURATION)
    if not math.isfinite(duration) or duration <= 0:
        return DEFAULT_UNIT_DURATION
    return float(duration)


def _bottom_levels(
    order: list[str],
    successors: Mapping[str, list[str]],
    durations: Mapping[str, float],
) -> dict[str, float]:
    bottom: dict[str, float] = {}
    for node in reversed(order):
        bottom[node] = _duration_of(durations, node) + max(
            (bottom.get(target, 0.0) for target in successors[node]), default=0.0
        )
    return bottom


def _downstream_counts(
    order: list[str], successors: Mapping[str, list[str]], exact: bool
) -> dict[str, int]:
    if not exact:
        return {node: len(targets) for node, targets in successors.items()}
    reachable: dict[str, set[str]] = {}
    for node in reversed(order):
        reach = set(successors[node])
        for target in successors[node]:
            reach |= reachable.get(target, frozenset())
        reach.discard(node)
        reachable[node] = reach
    return {node: len(reach) for node, reach in reachable.items()}


def compute_precedence_ranks(
    tasks: Iterable[Task], durations: Mapping[str, float] | None = None
) -> PrecedenceRanks:
    """Precedence DAGのbottom level・直接後続数・到達可能後続数を求める。

    `durations`はsubtask_id -> 推定所要時間（秒）。欠けているノードは
    `DEFAULT_UNIT_DURATION`として扱う。同じ入力からは常に同じ結果を返す。
    """
    task_list = list(tasks)
    node_ids, successors = _successor_map(task_list)
    order, has_cycle = _topological_order(node_ids, successors)
    exact = len(node_ids) <= MAX_TRANSITIVE_CLOSURE_NODES and not has_cycle
    return PrecedenceRanks(
        bottom_level=_bottom_levels(order, successors, durations or {}),
        unlocked={node: len(targets) for node, targets in successors.items()},
        downstream=_downstream_counts(order, successors, exact),
        exact_downstream=exact,
    )
