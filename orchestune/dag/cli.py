"""Command-line interface for DAG validation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from orchestune.dag.graph import build_dag_from_plan
from orchestune.dag.models import (
    ConfigError,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
    resolve_repo_root,
)
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD


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
        f"{DEFAULT_SIMILARITY_THRESHOLD}, i.e. orchestune.dag.similarity.DEFAULT_SIMILARITY_THRESHOLD)",
    )
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
    warnings = dag.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  {warning}")


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        # footprintはリポジトリルートからの相対パスとして定義されているため、
        # --planの位置から.gitを上方探索する。Git管理外の場合のみ、
        # 呼び出し元のcwdではなく--planファイル自身の位置を基点にする。
        repo_root = resolve_repo_root(args.plan)
        config = load_orchestune_config(repo_root)
        extra_ignore_patterns = compile_extra_ignore_patterns(
            extract_dag_ignore_patterns(config)
        )
        # 優先順位: --threshold（CLI） > dag_similarity_threshold（設定ファイル、
        # #407）> DEFAULT_SIMILARITY_THRESHOLD。設定ファイルへ永続化した値は
        # orchestune-provisionからも同じ関数で読まれ、両ツールが同じ閾値で
        # 再計算するため、`orchestune-dag --threshold`で意図的に消したエッジが
        # provision側で復活する食い違いを防ぐ。
        config_threshold = extract_dag_similarity_threshold(config)
        threshold = (
            args.threshold
            if args.threshold is not None
            else config_threshold
            if config_threshold is not None
            else DEFAULT_SIMILARITY_THRESHOLD
        )
        # 類似度スコア（重み付きOtsuka-Ochiai係数）は[0, 1]の範囲に収まるため、
        # 域外の値（NaN/Inf/範囲外の有限値）は全エッジが黙って抑制される
        # だけの無意味な指定になる。明示的にエラーとして拒否する。
        if not (0 <= threshold <= 1):
            raise ConfigError(
                f"--threshold must be within [0, 1] (a similarity score), got {threshold}"
            )
    except ConfigError as error:
        # #428: config-derived errors (orchestune.toml / pyproject.toml
        # values, or the --threshold flag validated against them) exit 2,
        # matching orchestune-dispatch's `_config_error` convention, so
        # automation can tell "bad config" apart from other failures below.
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    try:
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
