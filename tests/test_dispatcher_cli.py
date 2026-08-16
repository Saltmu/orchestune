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

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import CycleReport, run_dispatch_cycle
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_state import RunState, load_run_state, save_run_state
from orchestune.dispatcher import main
from orchestune.forge import ForgeAuthError
from orchestune.models import IssueRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default():
    """テスト環境において GitHubForge.check_auth() が実際の gh 認証エラーを
    投げないように、デフォルトで pass するようにスタブする。"""
    with patch("orchestune.forge.GitHubForge.check_auth") as mock_check:
        yield mock_check


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default():
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    with (
        patch(
            "orchestune.forge.GitHubForge.get_label_actor",
            return_value="trusted-actor",
        ),
        patch(
            "orchestune.forge.GitHubForge.get_actor_permission",
            return_value="write",
        ),
    ):
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
            "zombie-gc": False,
        }
        defaults = _config_defaults(parser, config_data)
        assert defaults["task_timeout_seconds"] == 1200
        assert defaults["zombie_gc"] is False

    def test_config_defaults_validation_error(self):
        import pytest

        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"task-timeout-seconds": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"zombie-gc": "invalid"})

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

        assert mock_build.call_args.args[0] == "auto"

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

        assert mock_build.call_args.args[0] == "cloud-routine"

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

        assert mock_build.call_args.args[0] == "local"


class TestDispatcherConfigLoading:
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

    def test_load_config_from_orchestune_toml(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "dispatch-target = 'local'\n"
            "run-state-path = 'custom_state.json'\n"
            "events-log-path = 'custom_events.jsonl'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "local"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 5
        assert config_arg.run_state_path == Path("custom_state.json")

    def test_orchestune_toml_with_dag_ignore_patterns_does_not_crash_dispatcher(
        self, tmp_path
    ):
        """#398/#404レビュー指摘の再現・回帰防止: orchestune-dag向けの
        `dag_ignore_patterns`が同じ`orchestune.toml`に存在しても、
        `orchestune-dispatch`本体はクラッシュせず通常どおり動作すること。"""
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "events-log-path = 'custom_events.jsonl'\n"
            'dag_ignore_patterns = ["(^|/)package.json$"]\n',
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 5
        assert len(config_arg.dag_ignore_patterns) == 1
        assert config_arg.dag_ignore_patterns[0].search("package.json")
        assert config_arg.dag_ignore_patterns[0].search("src/package.json")
        assert config_arg.dag_ignore_patterns[0].search("other.json") is None

    def test_orchestune_toml_with_dag_similarity_threshold_is_forwarded(self, tmp_path):
        """#407/#415レビュー指摘: dag_similarity_thresholdもdag_ignore_patterns
        と同様に読み込まれ、DispatcherConfig（ひいては実行時DAG再計算・
        integrator）へ実際に反映されること（無視されるだけで終わらないこと）。"""
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "events-log-path = 'custom_events.jsonl'\n"
            "dag_similarity_threshold = 0.1\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        config_arg = mock_run.call_args.args[0]
        assert config_arg.dag_similarity_threshold == 0.1

    def test_dag_similarity_threshold_defaults_when_unset(self, tmp_path):
        """#407/#415: 設定ファイル未指定時はDEFAULT_SIMILARITY_THRESHOLDのまま
        （既定挙動を変えない）。"""
        from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD

        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        config_arg = mock_run.call_args.args[0]
        assert config_arg.dag_similarity_threshold == DEFAULT_SIMILARITY_THRESHOLD

    def test_invalid_dag_similarity_threshold_in_config_is_reported_as_error(
        self, tmp_path, capsys
    ):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "dag_similarity_threshold = 2\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as excinfo:
            main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
        assert "[0, 1]" in capsys.readouterr().err

    def test_hyphenated_dag_ignore_patterns_key_is_also_honored(self, tmp_path):
        """#404レビュー指摘の再現・回帰防止: この設定ファイルの他のキーは
        全てハイフン区切り（max-concurrent等）のため、その慣習に沿って
        `dag-ignore-patterns`と書いた場合もサイレントに無視されず、
        `dag_ignore_patterns`と同様に効くこと。"""
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "events-log-path = 'custom_events.jsonl'\n"
            'dag-ignore-patterns = ["(^|/)package.json$"]\n',
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        config_arg = mock_run.call_args.args[0]
        assert len(config_arg.dag_ignore_patterns) == 1
        assert config_arg.dag_ignore_patterns[0].search("package.json")

    def test_invalid_dag_ignore_patterns_in_config_is_reported_as_error(
        self, tmp_path, capsys
    ):
        """#398/#404: dag_ignore_patternsの型・正規表現が不正な場合も、
        他のdispatcher設定エラーと同様にexit code 2で明示的に報告すること。"""
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            'dag_ignore_patterns = "not-a-list"\n',
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert "dag_ignore_patterns" in capsys.readouterr().err

    def test_load_config_from_pyproject_toml(self, tmp_path):
        config_path = tmp_path / "pyproject.toml"
        config_path.write_text(
            "[tool.orchestune]\n"
            "max-concurrent = 7\n"
            "dispatch-target = 'claude-cli'\n"
            "events-log-path = 'custom_events.jsonl'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 7

    def test_cli_arg_overrides_config_file(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n" "dispatch-target = 'local'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--max-concurrent",
                    "3",
                    "--dispatch-target",
                    "claude-cli",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 3

    def test_ci_command_cli_flag_is_split_into_argv_list(self, tmp_path):
        """#394: `--ci-command`はshlex構文の文字列として受け取り、
        `DispatcherConfig.ci_command`にはargvリストとして渡ること。"""
        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--ci-command",
                    "make ci",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        config_arg = mock_run.call_args.args[0]
        assert config_arg.ci_command == ["make", "ci"]

    def test_ci_command_unset_defaults_to_none(self, tmp_path):
        """#394: `--ci-command`未指定時は`DispatcherConfig.ci_command`が
        `None`のままで、Integrator側の既定値フォールバックに委ねる（後方互換）。"""
        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        config_arg = mock_run.call_args.args[0]
        assert config_arg.ci_command is None

    def test_ci_command_loaded_from_orchestune_toml(self, tmp_path):
        """#394: `orchestune.toml`の`ci-command`からも設定できること。"""
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            'ci-command = "npm run ci"\nevents-log-path = "custom_events.jsonl"\n',
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target"),
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        config_arg = mock_run.call_args.args[0]
        assert config_arg.ci_command == ["npm", "run", "ci"]

    @pytest.mark.parametrize(
        ("config", "expected_error"),
        [
            ("dispatch-target = 'claude-clii'\n", "dispatch-target"),
            ("max-concurent = 5\n", "unknown key"),
            ('max-concurrent = "5"\n', "must be an integer"),
            ('apply = "false"\n', "must be a boolean"),
            ("max-concurrent = -1\n", "greater than or equal to 0"),
            ("window-seconds = 0\n", "greater than or equal to 1"),
            ("parent-issue = 0\n", "greater than or equal to 1"),
            ("run-state-path = 1\n", "must be a string path"),
        ],
    )
    def test_rejects_invalid_config_values(
        self, tmp_path, config, expected_error, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(config, encoding="utf-8")

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert expected_error in capsys.readouterr().err

    def test_rejects_invalid_toml_without_falling_back_to_pyproject(
        self, tmp_path, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(
            "max-concurrent = [\n", encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\nmax-concurrent = 5\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert "failed to load" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "dispatch_target",
        [
            "local",
            "cloud-routine",
            "codex-cloud",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "auto",
        ],
    )
    def test_accepts_each_dispatch_target(self, tmp_path, dispatch_target):
        (tmp_path / "orchestune.toml").write_text(
            f"dispatch-target = '{dispatch_target}'\nevents-log-path = 'custom_events.jsonl'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply"], cwd=tmp_path)

        assert mock_build.call_args.args[0] == dispatch_target

    def test_post_cycle_failures_in_main(self, tmp_path, capsys):
        r1 = PhaseResult("poll_pending_not_needed_reviews", PhaseStatus.SUCCESS)
        r2 = PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS)
        r2_retryable = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.RETRYABLE_FAILURE,
            error_message="retryable-error",
            retryable=True,
        )
        r2_fatal = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.FATAL_FAILURE,
            error_message="fatal-error",
        )
        r3 = PhaseResult("process_parent_completion", PhaseStatus.SUCCESS)
        r4 = PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS)

        # ケース1: すべて成功
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch("orchestune.dispatcher._run_semantic_integrator", return_value=r2),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
            patch("orchestune.dispatcher._post_event_log_comment", return_value=r4),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 0
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 4
            assert out["post_cycle_results"][0]["status"] == "success"

        # ケース2: RETRYABLE_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator",
                return_value=r2_retryable,
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
            patch("orchestune.dispatcher._post_event_log_comment", return_value=r4),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 2
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "retryable_failure"
            assert out["post_cycle_results"][1]["error_message"] == "retryable-error"

        # ケース3: FATAL_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator", return_value=r2_fatal
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
            patch("orchestune.dispatcher._post_event_log_comment", return_value=r4),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "fatal_failure"

        # ケース4: main()のcheck_auth()自体がForgeAuthErrorを投げる場合
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.forge.GitHubForge.check_auth",
                side_effect=ForgeAuthError("main-auth-failed"),
            ),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 4
            for res in out["post_cycle_results"]:
                assert res["status"] == "fatal_failure"
                assert "main-auth-failed" in res["error_message"]

    def test_post_event_log_comment_not_called_without_parent_issue(self, tmp_path):
        """#396: `--parent-issue`未指定時は`_post_event_log_comment`自体が
        呼ばれない（投稿先の単一Issueが定まらないため、既存挙動と同じガード）。"""
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=PhaseResult(
                    "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator",
                return_value=PhaseResult(
                    "run_semantic_integrator", PhaseStatus.SUCCESS
                ),
            ),
            patch("orchestune.dispatcher._post_event_log_comment") as mock_post,
        ):
            code = main(
                [
                    "--apply",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        assert code == 0
        mock_post.assert_not_called()

    def test_post_event_log_comment_receives_cycle_report(self, tmp_path):
        """#396: `run_dispatch_cycle`が返した`CycleReport`が、そのまま
        `_post_event_log_comment`へ渡されること。"""
        cycle_report = self._empty_report()
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=cycle_report,
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=PhaseResult(
                    "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator",
                return_value=PhaseResult(
                    "run_semantic_integrator", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatcher._process_parent_completion",
                return_value=PhaseResult(
                    "process_parent_completion", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatcher._post_event_log_comment",
                return_value=PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS),
            ) as mock_post,
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )

        assert code == 0
        mock_post.assert_called_once()
        assert mock_post.call_args.args[1] is cycle_report

    def test_custom_window_seconds_preserves_launch_history_quota(self, tmp_path):
        now = time.time()
        # window_seconds = 172800 (48時間)
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=172800,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # 36時間前 (129600秒前) の launch 記録（24時間前〜48時間前の間）
        launch_36h_ago = now - 129600.0
        launch_1h_ago = now - 3600.0

        save_run_state(
            RunState(
                launch_history=[launch_36h_ago, launch_1h_ago],
            ),
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
        )

        # 48時間の window_seconds なので、36時間前の起動も記録に残っているはず
        loaded = load_run_state(config.run_state_path)
        assert len(loaded.launch_history) == 2

        # 2回起動済み（max_launches_per_window=2）のため新規起動がブロックされる
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            mock_list.side_effect = lambda label, **_: (
                [_issue(10, subtask_id="t10")] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)
            # 48時間窓で2回に達しているため起動不可
            assert len(report.selected) == 0

    def test_unsafe_cli_without_allow_unsafe_option_in_main_raises_config_error(
        self, tmp_path, capsys
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(["--dispatch-target", "claude-cli"], cwd=tmp_path)
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid dispatcher config" in err
        assert "完全権限実行となります" in err

    def test_unsafe_cli_with_allow_unsafe_option_in_main_succeeds(self, tmp_path):
        with patch(
            "orchestune.dispatcher.run_dispatch_cycle",
            return_value=self._empty_report(),
        ):
            code = main(
                [
                    "--dispatch-target",
                    "claude-cli",
                    "--allow-unsafe-agent-execution",
                    "--no-apply",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 0

    def test_unsafe_cli_with_allow_unsafe_option_in_orchestune_toml_succeeds(
        self, tmp_path
    ):
        orchestune_toml = tmp_path / "orchestune.toml"
        orchestune_toml.write_text(
            "allow_unsafe_agent_execution = true\n", encoding="utf-8"
        )
        with patch(
            "orchestune.dispatcher.run_dispatch_cycle",
            return_value=self._empty_report(),
        ):
            code = main(
                [
                    "--dispatch-target",
                    "claude-cli",
                    "--no-apply",
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ],
                cwd=tmp_path,
            )
            assert code == 0
