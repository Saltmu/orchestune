"""Command-line interface for DAG validation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from orchestune.dag_graph import build_dag_from_plan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="orchestune DAG validation tool")
    parser.add_argument(
        "--plan",
        default="decomposition_plan.md",
        help="Path to the decomposition plan markdown file (default: decomposition_plan.md)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    return parser


def _print_text_result(dag: dict[str, Any]) -> None:
    topological_order = dag["topological_order"]
    parallel_leaves = dag["parallel_leaves"]
    risky_subtask_ids = dag["risky_subtask_ids"]
    edges = dag["edges"]
    print("DAG validation succeeded.")
    print(f"Topological order: {' -> '.join(topological_order)}")
    print(f"Parallel leaves: {', '.join(parallel_leaves)}")
    if risky_subtask_ids:
        print(f"Risky subtasks: {', '.join(risky_subtask_ids)}")
    if edges:
        print("Edges:")
        for edge in edges:
            score = edge.get("score")
            score_text = f" (score: {score:.2f})" if score is not None else ""
            print(
                f"  {edge['source']} -> {edge['target']} "
                f"[reason: {edge['reason']}{score_text}]"
            )


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        dag = build_dag_from_plan(args.plan)
        if args.json:
            print(json.dumps(dag, indent=2))
        else:
            _print_text_result(dag)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    raise SystemExit(0)
