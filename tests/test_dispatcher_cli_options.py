"""dispatcherのCLI引数パース・設定ファイル読み込みテスト。

`tests/test_dispatcher.py`の肥大化解消のため分割している（#349）。
`run_dispatch_cycle`自体のディスパッチループ統合テストは
`test_dispatcher_pipeline.py`へ、post-cycleのベストエフォート処理本体
（`_run_best_effort_phase`とその利用箇所）は`dispatch_postcycle`モジュールの
新設に合わせて`test_dispatch_postcycle.py`へそれぞれ分割し、本ファイルには
`_build_arg_parser`/`_config_defaults`/`main`のCLI引数・設定ファイル関連の
挙動のみを残している（`main`がpost-cycleフェーズをオーケストレーションする
配線自体は`TestDispatcherConfigLoading.test_post_cycle_failures_in_main`で
検証を続ける）。
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch_cycle import CycleReport
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatcher import main
from orchestune.models import IssueRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default(fake_forge):
    """テスト環境において GitHubForge.check_auth() が実際の gh 認証エラーを
    投げないように、デフォルトで pass するようにスタブする。"""
    fake_forge.check_auth.reset_mock(side_effect=True)
    mock_check = fake_forge.check_auth
    yield mock_check


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    fake_forge.get_label_actor.reset_mock(side_effect=True)
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.reset_mock(side_effect=True)
    fake_forge.get_actor_permission.return_value = "write"
    yield


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    footprint_lines = "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
    symbols_lines = "\n".join(f"  - {s}" for s in symbols) if symbols else "  []"
    depends_on_lines = (
        "\n".join(f"  - {d}" for d in depends_on) if depends_on else "  []"
    )
    body = (
        "## Footprint\n"
        "```yaml\n"
        f"subtask_id: {subtask_id}\n"
        "footprint:\n"
        f"{footprint_lines}\n"
        "symbols:\n"
        f"{symbols_lines}\n"
        "depends_on:\n"
        f"{depends_on_lines}\n"
        "```\n"
    )
    parent = {"number": parent_number} if parent_number is not None else None
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=labels,
        created_at=created_at,
        parent=parent,
    )


class TestBuildArgParser:
    """#328: dispatch-cycleの既定挙動をapplyに変更（--no-applyでdry-run）。"""

    def test_apply_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.apply is True

    def test_no_apply_flag_disables_apply(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-apply"])
        assert args.apply is False

    def test_max_tokens_args_defaults_to_none(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.max_tokens_per_window is None
        assert args.max_tokens_per_task is None

    def test_max_tokens_args_are_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(
            [
                "--max-tokens-per-window",
                "50000",
                "--max-tokens-per-task",
                "10000",
            ]
        )
        assert args.max_tokens_per_window == 50000
        assert args.max_tokens_per_task == 10000

    def test_explicit_apply_flag_still_works(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--apply"])
        assert args.apply is True

    def test_dispatch_target_defaults_to_none_when_unspecified(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.dispatch_target is None

    def test_dispatch_target_explicit_local_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "local"])
        assert args.dispatch_target == "local"

    def test_dispatch_target_explicit_auto_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "auto"])
        assert args.dispatch_target == "auto"

    def test_dispatch_target_explicit_codex_cli_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cli"])
        assert args.dispatch_target == "codex-cli"

    def test_dispatch_target_explicit_codex_cloud_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cloud"])
        assert args.dispatch_target == "codex-cloud"

    def test_codex_cloud_env_option_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--codex-cloud-env", "env_123"])
        assert args.codex_cloud_env == "env_123"

    def test_task_timeout_seconds_defaults_to_zero(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.task_timeout_seconds == 0

    def test_task_timeout_seconds_arg_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--task-timeout-seconds", "3600"])
        assert args.task_timeout_seconds == 3600

    def test_max_task_reclaims_defaults_to_a_finite_value(self):
        """#512: 「無制限」を表す既定値を持たない（終端のない経路を作らない）。"""
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.max_task_reclaims == 3

    def test_max_task_reclaims_arg_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--max-task-reclaims", "5"])
        assert args.max_task_reclaims == 5

    def test_max_task_reclaims_rejects_negative_values(self):
        """PR#520レビュー対応(Codex P2): 負値は設定ファイル同様CLIでも拒否する
        （素通りすると「1回目の回収で必ず上限超過」と解釈され、タスクが黙って
        status:blocked-human-reviewへ落ちてしまう）。"""
        import pytest

        from orchestune.dispatcher import _build_arg_parser

        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--max-task-reclaims", "-1"])

    def test_not_needed_review_timeout_seconds_defaults_to_a_finite_value(self):
        """#511: 「無制限」を表す既定値を持たない（終端のない経路を作らない）。"""
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.not_needed_review_timeout_seconds == 86400

    def test_not_needed_review_timeout_seconds_arg_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(
            ["--not-needed-review-timeout-seconds", "1800"]
        )
        assert args.not_needed_review_timeout_seconds == 1800

    def test_not_needed_review_timeout_seconds_rejects_negative_values(self):
        """`--max-task-reclaims`と同じ理由で負値を拒否する（素通りすると
        「今すぐ全件エスカレーション」相当になり、保留エントリが黙って
        status:blocked-human-reviewへ落ちてしまう）。"""
        import pytest

        from orchestune.dispatcher import _build_arg_parser

        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(
                ["--not-needed-review-timeout-seconds", "-1"]
            )

    def test_zombie_gc_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.zombie_gc is True

    def test_no_zombie_gc_disables_zombie_gc(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-zombie-gc"])
        assert args.zombie_gc is False


class TestDispatcherCliSingleResponsibility:
    def test_parser_option_groups_preserve_all_destinations(self):
        from orchestune.dispatcher import (
            _add_dispatch_target_arguments,
            _add_execution_arguments,
            _add_safety_and_budget_arguments,
            _add_storage_arguments,
        )

        parser = __import__("argparse").ArgumentParser()
        for register in (
            _add_execution_arguments,
            _add_storage_arguments,
            _add_dispatch_target_arguments,
            _add_safety_and_budget_arguments,
        ):
            register(parser)

        destinations = {action.dest for action in parser._actions}
        assert {
            "apply",
            "max_concurrent",
            "run_state_path",
            "events_log_path",
            "not_needed_review_state_path",
            "dispatch_target",
            "routine_token",
            "allow_unsafe_agent_execution",
            "max_tokens_per_task",
            "ci_command",
        } <= destinations

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            ([], 0),
            ([PhaseStatus.SUCCESS], 0),
            ([PhaseStatus.RETRYABLE_FAILURE], 2),
            ([PhaseStatus.RETRYABLE_FAILURE, PhaseStatus.FATAL_FAILURE], 1),
        ],
    )
    def test_post_cycle_exit_code_is_calculated_independently(self, statuses, expected):
        from orchestune.dispatcher import _post_cycle_exit_code

        results = [PhaseResult(phase_name="test", status=status) for status in statuses]
        assert _post_cycle_exit_code(results) == expected


class TestConfigDefaults:
    def test_config_defaults_load(self):
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        config_data = {
            "task-timeout-seconds": 1200,
            "max-task-reclaims": 5,
            "not-needed-review-timeout-seconds": 1800,
            "zombie-gc": False,
        }
        defaults = _config_defaults(parser, config_data)
        assert defaults["task_timeout_seconds"] == 1200
        assert defaults["max_task_reclaims"] == 5
        assert defaults["not_needed_review_timeout_seconds"] == 1800
        assert defaults["zombie_gc"] is False

    def test_config_defaults_validation_error(self):
        import pytest

        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"task-timeout-seconds": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"zombie-gc": "invalid"})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"max-task-reclaims": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"not-needed-review-timeout-seconds": -1})

    def test_dag_ignore_patterns_key_is_ignored_not_rejected(self):
        """#398/#404レビュー指摘: orchestune-dag CLIが同じ設定ファイル
        （orchestune.toml/[tool.orchestune]）から読む`dag_ignore_patterns`は
        dispatcher自身の引数ではないため、他のtypoと違って"unknown key"には
        せず無視して処理を継続できること。"""
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        defaults = _config_defaults(
            parser,
            {
                "dag_ignore_patterns": ["(^|/)package.json$"],
                "max-concurrent": 3,
            },
        )
        assert "dag_ignore_patterns" not in defaults
        assert defaults["max_concurrent"] == 3

    def test_dag_similarity_threshold_key_is_ignored_not_rejected(self):
        """#407: orchestune-dag/orchestune-provisionが同じ設定ファイルから
        読む`dag_similarity_threshold`も、dag_ignore_patternsと同様に
        dispatcher自身の引数ではないため"unknown key"にせず無視できること。
        `_NON_DISPATCHER_CONFIG_KEYS`の完全一致リストへ個別に追記する方式
        だと、こうした「他ツール専用の新規キー」が増えるたびに手動追記が
        必要になる（Issue #407 項目3）。"""
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        defaults = _config_defaults(
            parser,
            {
                "dag_similarity_threshold": 0.5,
                "max-concurrent": 3,
            },
        )
        assert "dag_similarity_threshold" not in defaults
        assert defaults["max_concurrent"] == 3

    def test_hyphenated_dag_similarity_threshold_key_is_also_ignored(self):
        """`dag-similarity-threshold`エイリアス表記でも同様に無視されること。"""
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        defaults = _config_defaults(
            parser,
            {"dag-similarity-threshold": 0.5},
        )
        assert "dag_similarity_threshold" not in defaults
        assert "dag-similarity-threshold" not in defaults

    def test_misspelled_dag_key_is_still_rejected(self):
        """#415レビュー指摘: `dag_`prefixだけで無条件に許可すると、
        `dag_similarity_treshold`（スペルミス）や`dag_ignore_pattern`
        （末尾のs脱落）のようなtypoも黙って無視され、設定が効いていない
        ことにユーザーが気づけない。既知の共有DAGキー名の完全一致でのみ
        無視し、それ以外の`dag_`始まりキーは引き続き"unknown key"として
        拒否すること。"""
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag_similarity_treshold": 0.5})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag_ignore_pattern": ["a.py"]})

    def test_mixed_separator_dag_key_is_still_rejected(self):
        """#415レビュー再指摘: 区切り文字が混在したtypo（`dag_similarity-threshold`
        や`dag-ignore_patterns`）は、`_normalize_config_key`のハイフン→
        アンダースコア正規化を経ると許可リストの正確な表記
        （`dag_similarity_threshold`/`dag_ignore_patterns`）に一致して
        しまい、"unknown key"検知をすり抜けてしまう。しかも
        `extract_dag_similarity_threshold`/`extract_dag_ignore_patterns`は
        元のキー文字列（正規化前）でしか値を読まないため、この場合は
        気づかれないまま値が一切読み取られずデフォルトへフォールバックする
        （二重の見逃し）。混在表記は正規のスペリングでは無いため、
        引き続き"unknown key"として拒否すること。"""
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag_similarity-threshold": 0.5})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag-ignore_patterns": ["a.py"]})

    def test_unrelated_unknown_key_is_still_rejected(self):
        """`dag_ignore_patterns`の許容リストがdispatcher自身の設定の
        typo検知（意図的な仕様）を無効化しないこと。"""
        import pytest

        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"max-concurent": 5})


class TestMainDispatchTargetAutoDetection:
    """#121: --dispatch-target未指定時、mainが実行環境に応じた実ディスパッチ先を
    build_dispatch_targetへ渡すことを検証する（ダミー動作への誤フォールバック防止）。"""

    def _empty_report(self):
        return CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )

    def test_defaults_to_auto_outside_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--no-apply",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ]
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "auto"

    def test_defaults_to_cloud_routine_in_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--no-apply",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ]
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "cloud-routine"

    def test_explicit_local_wins_even_inside_github_actions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ]
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "local"
