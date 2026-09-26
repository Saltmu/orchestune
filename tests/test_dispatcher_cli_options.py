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

from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.dispatcher import main
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from tests.dispatch_test_support import (
    stub_forge_check_auth,
    stub_label_actor_permission,
)

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default(fake_forge):
    """`GitHubForge.check_auth()`が実際のgh認証エラーを投げないようスタブする。"""
    return stub_forge_check_auth(fake_forge)


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119のactor権限検証が実際の`gh api`を叩かないようスタブする。"""
    stub_label_actor_permission(fake_forge)


@pytest.fixture(autouse=True)
def _resolve_legacy_temp_cwds(monkeypatch):
    """Use the linked checkout for workspace lookup in legacy temp-dir tests."""
    from orchestune.dispatch import dispatcher

    resolve = dispatcher._resolve_dispatch_shared_paths
    repository_cwd = Path(__file__).resolve().parents[1]

    def _resolve(args, cwd):
        if cwd is not None and not cwd.resolve().is_relative_to(repository_cwd):
            cwd = repository_cwd
        return resolve(args, cwd)

    monkeypatch.setattr(dispatcher, "_resolve_dispatch_shared_paths", _resolve)


class TestBuildArgParser:
    def _parse_args(self, args=()):
        from orchestune.dispatch.dispatcher import _build_arg_parser

        return _build_arg_parser().parse_args(["--parent-issue", "100", *args])

    def test_apply_defaults_to_none_in_parser(self):
        args = self._parse_args([])
        assert args.apply is None

    def test_apply_flags_parse_correctly(self):
        assert self._parse_args(["--apply"]).apply is True
        assert self._parse_args(["--no-apply"]).apply is False

    def test_parent_issue_options(self):
        from orchestune.dispatch.dispatcher import _build_arg_parser

        parser = _build_arg_parser()
        assert parser.parse_args(["-p", "100"]).parent_issue == 100
        assert parser.parse_args(["--parent-issue", "100"]).parent_issue == 100
        with pytest.raises(SystemExit):
            parser.parse_args(["-p", "0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["-p", "-1"])

    def test_dispatch_target_defaults_to_none_when_unspecified(self):
        args = self._parse_args([])
        assert args.dispatch_target is None

    @pytest.mark.parametrize(
        "target",
        [
            "local",
            "auto",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "cloud-routine",
            "codex-cloud",
        ],
    )
    def test_dispatch_target_choices(self, target):
        args = self._parse_args(["--dispatch-target", target])
        assert args.dispatch_target == target

    def test_dispatch_target_invalid_choice_rejected(self):
        with pytest.raises(SystemExit):
            self._parse_args(["--dispatch-target", "invalid"])

    def test_max_concurrent_options(self):
        args = self._parse_args([])
        assert args.max_concurrent is None
        args = self._parse_args(["--max-concurrent", "5"])
        assert args.max_concurrent == 5
        with pytest.raises(SystemExit):
            self._parse_args(["--max-concurrent", "-1"])

    def test_profile_option(self):
        args = self._parse_args([])
        assert args.profile is None
        args = self._parse_args(["--profile", "deep-reasoning"])
        assert args.profile == "deep-reasoning"

    def test_allow_unsafe_option(self):
        args = self._parse_args([])
        assert args.allow_unsafe_agent_execution is False
        args = self._parse_args(["--allow-unsafe-agent-execution"])
        assert args.allow_unsafe_agent_execution is True

    @pytest.mark.parametrize(
        "removed_option",
        [
            "--max-tokens-per-window",
            "--max-tokens-per-task",
            "--reviewer-bot",
            "--codex-cloud-env",
            "--task-timeout-seconds",
            "--max-task-reclaims",
            "--early-death-window-seconds",
            "--max-early-death-retries",
            "--early-death-backoff-seconds",
            "--not-needed-review-timeout-seconds",
            "--zombie-gc",
            "--no-zombie-gc",
            "--model",
            "--effort",
            "--reasoning-effort",
            "--ci-command",
            "--run-state-path",
            "--worktree-root",
            "--log-dir",
            "--events-log-path",
            "--not-needed-review-state-path",
            "--routine-id",
            "--routine-token",
            "--local-cmd",
            "--consistency-repair-code",
        ],
    )
    def test_removed_options_raise_error(self, removed_option):
        """#1035: 削除された日常外オプションはCLIで渡すとエラーとなる。"""
        flag_args = [removed_option]
        if removed_option not in {"--zombie-gc", "--no-zombie-gc"}:
            flag_args.append("val")
        with pytest.raises(SystemExit):
            self._parse_args(flag_args)


class TestDispatcherCliSingleResponsibility:
    def test_parser_option_groups_preserve_all_destinations(self):
        from orchestune.dispatch.dispatcher import _build_arg_parser

        parser = _build_arg_parser()
        destinations = {action.dest for action in parser._actions}
        assert destinations == {
            "help",
            "parent_issue",
            "apply",
            "dispatch_target",
            "max_concurrent",
            "profile",
            "allow_unsafe_agent_execution",
        }

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
        from orchestune.dispatch.dispatcher import _post_cycle_exit_code

        results = [PhaseResult(phase_name="test", status=status) for status in statuses]
        assert _post_cycle_exit_code(results) == expected


class TestConfigDefaults:
    def test_config_defaults_load(self):
        from orchestune.dispatch.config_loader import validate_toml_config

        config_data = {
            "task-timeout-seconds": 1200,
            "max-task-reclaims": 5,
            "not-needed-review-timeout-seconds": 1800,
            "zombie-gc": False,
            "reviewer-bot": "claude",
        }
        defaults = validate_toml_config(config_data)
        assert defaults["task_timeout_seconds"] == 1200
        assert defaults["max_task_reclaims"] == 5
        assert defaults["not_needed_review_timeout_seconds"] == 1800
        assert defaults["zombie_gc"] is False
        assert defaults["reviewer_bot"] == "claude"

    def test_cli_reviewer_bot_overrides_config_default(self):
        from orchestune.dispatch.dispatcher import _build_arg_parser

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--parent-issue", "100", "--reviewer-bot", "codex"])

    def test_config_defaults_validation_error(self):
        import pytest

        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"task-timeout-seconds": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"zombie-gc": "invalid"})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"reviewer-bot": "gemini"})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"max-task-reclaims": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"not-needed-review-timeout-seconds": -1})

    def test_dag_ignore_patterns_key_is_ignored_not_rejected(self):
        """#398/#404レビュー指摘: orchestune-dag CLIが同じ設定ファイル
        （orchestune.toml/[tool.orchestune]）から読む`dag_ignore_patterns`は
        dispatcher自身の引数ではないため、他のtypoと違って"unknown key"には
        せず無視して処理を継続できること。"""
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

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
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

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
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

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
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

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
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag_similarity-threshold": 0.5})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"dag-ignore_patterns": ["a.py"]})

    def test_unrelated_unknown_key_is_still_rejected(self):
        """`dag_ignore_patterns`の許容リストがdispatcher自身の設定の
        typo検知（意図的な仕様）を無効化しないこと。"""
        import pytest

        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "auto"

    def test_defaults_to_cloud_routine_in_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "cloud-routine"

    def test_explicit_local_wins_even_inside_github_actions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                ],
                cwd=tmp_path,
            )

        assert mock_build.call_args.args[0].dispatch_target_name == "local"

    def test_explicit_reviewer_bot_is_forwarded_to_target_builder(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "reviewer-bot = 'codex'\n", encoding="utf-8"
        )
        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )

        assert mock_build.call_args.args[0].reviewer_bot == "codex"

    def test_cli_model_and_effort_flags_removed(self, tmp_path):
        with pytest.raises(SystemExit):
            main(
                ["--parent-issue", "100", "--model", "claude-3-7-sonnet"], cwd=tmp_path
            )
        with pytest.raises(SystemExit):
            main(["--parent-issue", "100", "--effort", "high"], cwd=tmp_path)
        with pytest.raises(SystemExit):
            main(["--parent-issue", "100", "--reasoning-effort", "low"], cwd=tmp_path)

    def test_cli_profile_override_forwarded_to_dispatcher_config(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            'default_execution_profile = "deep-reasoning"\n[execution_profiles.deep-reasoning.local]\n',
            encoding="utf-8",
        )
        with patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=self._empty_report(),
        ) as mock_cycle:
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                    "--profile",
                    "deep-reasoning",
                ],
                cwd=tmp_path,
            )

        config = mock_cycle.call_args.args[0]
        assert config.profile == "deep-reasoning"
