"""#232: dispatch cycle後のベストエフォート後処理オーケストレーション。

`dispatch_cycle.run_dispatch_cycle`とは別の、`dispatcher.main()`から呼ばれる
ポストサイクルのワークフロー群（保留レビューのポーリング・意味的レビューを
含むIntegrator実行・親Issue完了処理）を集約する。いずれも失敗してもmain()の
続行を妨げないベストエフォート処理である。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_targets import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.forge import Forge, ForgeAuthError
from orchestune.integration_coordinator import (
    IntegrationCoordinator,
    process_pending_not_needed_reviews,
)
from orchestune.integrator import IntegrationStatus, Integrator, IntegratorConfig
from orchestune.parent_completion import process_parent_completion


def _decide_semantic_review_enabled() -> bool:
    """統合コーディネーターによる意味的レビュー（LLMによる統合PRのバグ検知、結果は
    PRコメントのみで完結）と、#282のstatus:not-needed独立検証レビューの両方を、
    `ORCHESTUNE_SEMANTIC_REVIEW=0`でまとめて無効化できる。"""
    return os.environ.get("ORCHESTUNE_SEMANTIC_REVIEW", "1") != "0"


def _run_best_effort_phase(
    *,
    phase_name: str,
    report_label: str,
    work: Callable[[], dict],
    auth_error: ForgeAuthError | None,
    auth_error_message: str,
    failure_message: str,
    evaluate_report: Callable[[dict], tuple[PhaseStatus, bool]] | None = None,
) -> PhaseResult:
    """#232: main()から呼ばれるベストエフォート後処理フェーズの共通実行部。

    `_poll_pending_not_needed_reviews`・`_run_semantic_integrator`・
    `_process_parent_completion`に重複していたtry/exceptボイラープレート
    （`ForgeAuthError`ならFATAL_FAILURE、その他例外ならRETRYABLE_FAILURE）を
    集約する。失敗してもここでは例外を投げず、`PhaseResult`として返すだけで
    main()の続行を妨げない。
    """
    try:
        if auth_error is not None:
            raise auth_error

        report = work()
        print(f"{report_label}:", file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)

        if evaluate_report is not None:
            status, retryable = evaluate_report(report)
        else:
            status, retryable = PhaseStatus.SUCCESS, False

        return PhaseResult(
            phase_name=phase_name, status=status, report=report, retryable=retryable
        )
    except ForgeAuthError as e:
        print(f"Error: {auth_error_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.FATAL_FAILURE,
            error_message=str(e),
            retryable=False,
        )
    except Exception as e:
        print(f"Warning: {failure_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.RETRYABLE_FAILURE,
            error_message=str(e),
            retryable=True,
        )


def _poll_pending_not_needed_reviews(
    args: argparse.Namespace,
    forge: Forge | None = None,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    """#282: status:not-needed判定の独立検証レビュー（保留分）をポーリングする。

    ベストエフォート処理: 失敗しても警告を出すだけでmain()は続行する。
    """

    def work() -> dict:
        return process_pending_not_needed_reviews(
            args.not_needed_review_state_path, forge=forge
        )

    return _run_best_effort_phase(
        phase_name="poll_pending_not_needed_reviews",
        report_label="Pending Not-Needed Review Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while polling reviews",
        failure_message="failed to process pending not-needed reviews",
    )


# Integratorパイプラインが成功として扱う唯一のステータス群（#207: これ以外は
# すべて失敗として扱うホワイトリスト方式。個別のエラーステータス追加時に
# 判定漏れが起きるブラックリスト方式を避けるため）。
_INTEGRATOR_SUCCESS_STATUSES = {
    IntegrationStatus.SUCCESS,
    IntegrationStatus.NO_DONE_TASKS,
}


def _run_semantic_integrator(
    config: DispatcherConfig,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    """統合コーディネーターによる意味的レビューを含め、Integratorを実行する。

    レビューはdispatcherと同一のクラウドルーチンを再利用して起動するため、
    実ディスパッチ先がクラウドルーチンのときのみ意味的レビューを有効化する。
    レビューセッションは統合PRへコメントを残すのみで、自動マージ等の後続処理は
    Python側では一切行わない。ベストエフォート処理: 失敗しても警告を出すだけで
    main()は続行する。
    """

    def work() -> dict:
        integrator_config = IntegratorConfig(
            parent_issue_number=config.parent_issue_number,
            apply=config.apply,
            forge=config.forge,
        )
        if semantic_review_enabled and isinstance(
            config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
        ):
            integrator_config.enable_semantic_review = True
            integrator_config.coordinator = IntegrationCoordinator(
                config.dispatch_target
            )
        else:
            integrator_config.enable_semantic_review = False
        # `_run_best_effort_phase`は`_process_parent_completion`等の異なる形の
        # reportとも共有されるため`work`は`Callable[[], dict]`のまま。
        # `IntegrationReport`(TypedDict)は`dict[Any, Any]`と型として
        # 互換にならないため、ここで素の`dict`へ変換する。
        return dict(Integrator(integrator_config).run())

    def evaluate_report(report: dict) -> tuple[PhaseStatus, bool]:
        if report.get("status") not in _INTEGRATOR_SUCCESS_STATUSES:
            return PhaseStatus.RETRYABLE_FAILURE, True
        return PhaseStatus.SUCCESS, False

    return _run_best_effort_phase(
        phase_name="run_semantic_integrator",
        report_label="Integrator Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while running Integrator",
        failure_message="Integrator failed to run",
        evaluate_report=evaluate_report,
    )


def _process_parent_completion(
    config: DispatcherConfig, auth_error: ForgeAuthError | None = None
) -> PhaseResult:
    """#170: 親Issue配下の全子Issue完了検知→最終PR用意、および最終PRの
    マージ検知→親Issueクローズを行う。ベストエフォート処理: 失敗しても警告を
    出すだけでmain()は続行する。
    """

    def work() -> dict:
        return process_parent_completion(
            config.parent_issue_number, config.apply, forge=config.forge
        )

    return _run_best_effort_phase(
        phase_name="process_parent_completion",
        report_label="Parent Completion Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while processing parent completion",
        failure_message="failed to process parent completion",
    )
