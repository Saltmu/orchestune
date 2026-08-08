"""Command-line interface for DAG validation."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from orchestune.dag_graph import build_dag_from_plan
from orchestune.dag_models import compile_extra_ignore_patterns
from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD

_DAG_IGNORE_PATTERNS_KEY = "dag_ignore_patterns"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="orchestune DAG validation tool")
    parser.add_argument(
        "--plan",
        default="decomposition_plan.md",
        help="Path to the decomposition plan markdown file (default: decomposition_plan.md)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Similarity edge threshold (default: "
        f"{DEFAULT_SIMILARITY_THRESHOLD}, i.e. orchestune.dag_similarity.DEFAULT_SIMILARITY_THRESHOLD)",
    )
    return parser


def _load_dag_ignore_patterns_config(repo_root: Path) -> list[str]:
    """Load extra footprint ignore patterns for `repo_root`.

    Looks up `orchestune.toml` (top-level `dag_ignore_patterns` key) first,
    falling back to `pyproject.toml`'s `[tool.orchestune]` table. Missing
    files/keys are not an error: an empty list keeps default behavior.
    """
    orchestune_toml = repo_root / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                config: dict[str, Any] = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"failed to load {orchestune_toml}: {e}") from e
    else:
        pyproject_toml = repo_root / "pyproject.toml"
        if not pyproject_toml.exists():
            return []
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"failed to load {pyproject_toml}: {e}") from e
        config = data.get("tool", {}).get("orchestune", {})
        if not isinstance(config, dict):
            raise ValueError(f"{pyproject_toml}: [tool.orchestune] must be a table")

    patterns = config.get(_DAG_IGNORE_PATTERNS_KEY, [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError(f"{_DAG_IGNORE_PATTERNS_KEY!r} must be a list of strings")
    return cast(list[str], patterns)


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
    warnings = dag.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  {warning}")


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        # footprintはリポジトリルートからの相対パスとして定義されているため、
        # 呼び出し元のcwdではなく--planファイル自身の位置を基点にする
        # （cwdが--planの置き場所と異なる場合、実在するファイルまで
        # 「見つからない」と誤検出してしまうため）。
        repo_root = Path(args.plan).resolve().parent
        extra_ignore_patterns = compile_extra_ignore_patterns(
            _load_dag_ignore_patterns_config(repo_root)
        )
        threshold = (
            args.threshold
            if args.threshold is not None
            else DEFAULT_SIMILARITY_THRESHOLD
        )
        dag = build_dag_from_plan(
            args.plan,
            threshold=threshold,
            repo_root=repo_root,
            ignore_patterns=extra_ignore_patterns,
        )
        if args.json:
            print(json.dumps(dag, indent=2))
        else:
            _print_text_result(dag)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    raise SystemExit(0)
