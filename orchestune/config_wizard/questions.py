"""Definitions of configuration questions, categories, and field metadata extraction."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal

from orchestune.dispatch.config import DispatcherConfig


@dataclass(frozen=True)
class QuestionSpec:
    key: str
    label: str
    help_text: str
    input_kind: Literal["choice", "text", "integer", "number", "string_list"]
    category: str
    choices: list[str] | None = None
    allow_unset: bool = True
    min_value: int | None = None
    max_value: int | None = None
    code_default_field: str | None = None

    def get_code_default(self) -> Any:
        if self.code_default_field is None:
            return None
        return _DISPATCHER_CONFIG_DEFAULTS.get(self.code_default_field)


def _extract_dispatcher_config_defaults() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for f in fields(DispatcherConfig):
        result[f.name] = f.default
    return result


_DISPATCHER_CONFIG_DEFAULTS = _extract_dispatcher_config_defaults()

BASIC_QUESTIONS: list[QuestionSpec] = [
    QuestionSpec(
        key="dispatch-target",
        label="実行先 (dispatch-target)",
        help_text=(
            "エージェントのディスパッチ先。未設定時は実行環境から自動選択。\n"
            "選択肢: auto, local (no-op), claude-cli, agy-cli, codex-cli, cloud-routine, codex-cloud"
        ),
        input_kind="choice",
        category="basic",
        choices=[
            "auto",
            "local",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "cloud-routine",
            "codex-cloud",
        ],
        allow_unset=True,
    ),
    QuestionSpec(
        key="ci-command",
        label="CIコマンド (ci-command)",
        help_text="タスク完了判定前に実行するCIコマンド（例: pytest, ./scripts/local-ci.sh）",
        input_kind="text",
        category="basic",
        allow_unset=True,
    ),
    QuestionSpec(
        key="max-concurrent",
        label="同時実行数 (max-concurrent)",
        help_text="同時に実行できるタスクの上限数（0以上の整数。0は無制限/スケジューラ既定）",
        input_kind="integer",
        category="basic",
        min_value=0,
        code_default_field="max_concurrent",
        allow_unset=False,
    ),
    QuestionSpec(
        key="max-launches-per-window",
        label="時間枠内起動上限 (max-launches-per-window)",
        help_text="指定時間枠内に新規起動できるタスクの上限（未設定=制限なし、0=新規起動禁止）",
        input_kind="integer",
        category="basic",
        min_value=0,
        code_default_field="max_launches_per_window",
        allow_unset=True,
    ),
    QuestionSpec(
        key="window-seconds",
        label="起動時間枠秒数 (window-seconds)",
        help_text="起動上限を判定する時間枠の秒数（1以上の整数）",
        input_kind="integer",
        category="basic",
        min_value=1,
        code_default_field="window_seconds",
        allow_unset=False,
    ),
    QuestionSpec(
        key="reviewer-bot",
        label="レビュー担当 (reviewer-bot)",
        help_text="PR自動レビューを担当するボット（未設定/auto/claude/codex）",
        input_kind="choice",
        category="basic",
        choices=["auto", "claude", "codex"],
        allow_unset=True,
    ),
]

ADVANCED_CATEGORIES = [
    ("timeouts", "タイムアウト・回収設定"),
    ("integration", "統合処理の上限"),
    ("tokens", "トークン上限"),
    ("paths", "保存先パス"),
    ("dag", "DAG設定"),
    ("profiles", "実行プロファイル"),
]

ADVANCED_QUESTIONS: dict[str, list[QuestionSpec]] = {
    "timeouts": [
        QuestionSpec(
            key="task-timeout-seconds",
            label="タスクタイムアウト秒数",
            help_text="タスク単体のタイムアウト秒数（0以上の整数）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="task_timeout_seconds",
        ),
        QuestionSpec(
            key="max-task-reclaims",
            label="タスク回収上限回数",
            help_text="タイムアウト等によるタスク自動再回収の上限回数（0以上）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="max_task_reclaims",
        ),
        QuestionSpec(
            key="early-death-window-seconds",
            label="早期終了検知ウィンドウ秒数",
            help_text="プロセス起動直後の早期クラッシュを検知する秒数（0以上）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="early_death_window_seconds",
        ),
        QuestionSpec(
            key="max-early-death-retries",
            label="早期終了再試行上限回数",
            help_text="早期終了時のリトライ上限回数（0以上）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="max_early_death_retries",
        ),
        QuestionSpec(
            key="early-death-backoff-seconds",
            label="早期終了バックオフ秒数",
            help_text="早期終了リトライ待機秒数（0以上）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="early_death_backoff_seconds",
        ),
        QuestionSpec(
            key="not-needed-review-timeout-seconds",
            label="不要判定レビュータイムアウト秒数",
            help_text="not-needed判定レビューのタイムアウト秒数（0以上）",
            input_kind="integer",
            category="timeouts",
            min_value=0,
            code_default_field="not_needed_review_timeout_seconds",
        ),
    ],
    "integration": [
        QuestionSpec(
            key="integration-dependency-timeout-seconds",
            label="依存解決タイムアウト秒数",
            help_text="統合処理の依存解決タイムアウト秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_dependency_timeout_seconds",
        ),
        QuestionSpec(
            key="integration-ci-timeout-seconds",
            label="統合CIタイムアウト秒数",
            help_text="統合処理のCIタイムアウト秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_ci_timeout_seconds",
        ),
        QuestionSpec(
            key="integration-cycle-timeout-seconds",
            label="統合サイクルタイムアウト秒数",
            help_text="統合処理全体のタイムアウト秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_cycle_timeout_seconds",
        ),
        QuestionSpec(
            key="integration-cleanup-timeout-seconds",
            label="統合クリーンアップタイムアウト秒数",
            help_text="統合処理後クリーンアップのタイムアウト秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_cleanup_timeout_seconds",
        ),
        QuestionSpec(
            key="integration-command-timeout-seconds",
            label="統合コマンドタイムアウト秒数",
            help_text="個別コマンド実行のタイムアウト秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_command_timeout_seconds",
        ),
        QuestionSpec(
            key="max-integration-timeout-retries",
            label="統合タイムアウトリトライ回数",
            help_text="タイムアウト時の再試行上限（0以上。0は初回タイムアウトで終了）",
            input_kind="integer",
            category="integration",
            min_value=0,
            code_default_field="max_integration_timeout_retries",
        ),
        QuestionSpec(
            key="integration-timeout-backoff-seconds",
            label="統合タイムアウトバックオフ秒数",
            help_text="タイムアウトリトライ待機秒数（1以上）",
            input_kind="integer",
            category="integration",
            min_value=1,
            code_default_field="integration_timeout_backoff_seconds",
        ),
    ],
    "tokens": [
        QuestionSpec(
            key="max-tokens-per-window",
            label="時間枠内トークン上限",
            help_text="指定時間枠内で消費可能なトークン総数（0以上、未設定=制限なし）",
            input_kind="integer",
            category="tokens",
            min_value=0,
            code_default_field="max_tokens_per_window",
        ),
        QuestionSpec(
            key="max-tokens-per-task",
            label="タスク単体トークン上限",
            help_text="タスク単体で消費可能なトークン数上限（0以上、未設定=制限なし）",
            input_kind="integer",
            category="tokens",
            min_value=0,
            code_default_field="max_tokens_per_task",
        ),
    ],
    "paths": [
        QuestionSpec(
            key="run-state-path",
            label="実行台帳パス (run-state-path)",
            help_text="Orchestuneの実行状態台帳ファイルのパス（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
        QuestionSpec(
            key="worktree-root",
            label="worktreeルート (worktree-root)",
            help_text="タスクworktreeを作成するルートディレクトリ（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
        QuestionSpec(
            key="log-dir",
            label="ログディレクトリ (log-dir)",
            help_text="ログ出力先ディレクトリ（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
        QuestionSpec(
            key="events-log-path",
            label="イベントログパス (events-log-path)",
            help_text="イベントログ出力ファイルのパス（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
        QuestionSpec(
            key="report-dir",
            label="レポートディレクトリ (report-dir)",
            help_text="ディスパッチレポート出力ディレクトリ（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
        QuestionSpec(
            key="not-needed-review-state-path",
            label="not-needed台帳パス",
            help_text="not-neededレビュー状態台帳のパス（相対パス推奨）",
            input_kind="text",
            category="paths",
        ),
    ],
}
