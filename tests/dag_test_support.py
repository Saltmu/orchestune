"""tests/test_dag_*.py群が共有するヘルパー・フィクスチャデータ。

test_dag.py (1050行) を、パース・類似度・グラフ構築の3つの関心事別に
test_dag_parsing.py / test_dag_similarity.py / test_dag_graph.py へ分割した際
(#479)、各ファイルから共通利用される`_write_plan`/`BASIC_PLAN`/`_subtask`を
このモジュールへ切り出した。`test_`で始まらないためpytestには収集されない。
"""

import textwrap

from orchestune.dag.models import SubTask


def _write_plan(tmp_path, content: str):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


BASIC_PLAN = """\
---
subtasks:
  - id: task-a
    description: "Aを実装する"
    footprint:
      - src/foo.py
    symbols:
      - foo.Foo
    depends_on: []
  - id: task-b
    description: "Bを実装する"
    footprint:
      - src/bar.py
    symbols:
      - bar.helper
    depends_on: ["task-a"]
---

# Decomposition Plan

本文は自由記述のためパース対象外。
"""


def _subtask(id_, footprint, symbols, depends_on=(), priority="medium"):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=tuple(symbols),
        depends_on=tuple(depends_on),
        risk=False,
        risk_reasons=(),
        priority=priority,
    )
