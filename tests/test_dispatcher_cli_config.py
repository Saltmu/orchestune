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

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import CycleReport, run_dispatch_cycle
from orchestune.dispatch.dispatcher import main
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.state import RunState, load_run_state
from orchestune.forge import ForgeAuthError
from tests.dispatch_test_support import make_footprint_issue as _issue
from tests.dispatch_test_support import (
    save_locked_run_state as save_run_state,
)
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
    """Keep isolated config fixtures while exercising the repository contract.

    Most pre-#966 tests put their temporary config file outside Git and pass that
    directory as ``cwd``. The production contract now rejects that arrangement;
    route those legacy tests' workspace lookup through this linked checkout while
    leaving the dedicated outside-repository regression test in its own module untouched.
    """
    from orchestune.dispatch import dispatcher

    resolve = dispatcher._resolve_dispatch_shared_paths
    repository_cwd = Path.cwd()

    def _resolve(args, cwd):
        if cwd is not None and not cwd.resolve().is_relative_to(repository_cwd):
            cwd = repository_cwd
        return resolve(args, cwd)

    monkeypatch.setattr(dispatcher, "_resolve_dispatch_shared_paths", _resolve)


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

    def test_parent_issue_is_required_when_absent_from_cli_and_config(self, tmp_path):
        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2

    def test_dispatcher_config_requires_parent_issue_number(self):
        with pytest.raises(TypeError, match="parent_issue_number"):
            DispatcherConfig()

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--parent-issue", "181", "--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0].dispatch_target_name == "local"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 5
        assert config_arg.parent_issue_number == 181
        from orchestune.claim.workspace import resolve_claim_workspace

        repository_root = resolve_claim_workspace(Path.cwd()).common_dir.parent
        assert (
            config_arg.run_state_path
            == (repository_root / "custom_state.json").resolve()
        )

    def test_scheduling_mode_in_the_config_file_is_rejected_as_unknown(self, tmp_path):
        """#752: scheduling-mode は削除されたため設定ファイルにあると未知キーとして拒否される。"""
        (tmp_path / "orchestune.toml").write_text(
            "scheduling-mode = 'critical-path'\nevents-log-path = 'custom_events.jsonl'\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

    def test_consistency_repair_is_configurable_from_the_config_file(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "consistency-mode = 'repair'\n"
            "consistency-repair-code = ['status.primary-status-conflict']\n"
            "consistency-max-repair-passes = 2\n"
            "events-log-path = 'custom_events.jsonl'\n",
            encoding="utf-8",
        )

        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

        config = mock_run.call_args.args[0]
        assert config.consistency_mode.value == "repair"
        assert config.consistency_repair_allowlist == frozenset(
            {"status.primary-status-conflict"}
        )
        assert config.consistency_max_repair_passes == 2

    def test_consistency_repair_pass_bound_is_validated_in_config_file(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "consistency-max-repair-passes = 6\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

    def test_cli_repair_allowlist_flag_is_removed_from_cli(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--consistency-repair-code",
                    "status.from-cli",
                ],
                cwd=tmp_path,
            )
        assert excinfo.value.code == 2

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

        config_arg = mock_run.call_args.args[0]
        assert config_arg.dag_similarity_threshold == 0.1

    def test_dag_similarity_threshold_defaults_when_unset(self, tmp_path):
        """#407/#415: 設定ファイル未指定時はDEFAULT_SIMILARITY_THRESHOLDのまま
        （既定挙動を変えない）。"""
        from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD

        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
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
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0].dispatch_target_name == "claude-cli"
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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ) as mock_build,
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--max-concurrent",
                    "3",
                    "--dispatch-target",
                    "claude-cli",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0].dispatch_target_name == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 3

    def test_ci_command_cli_flag_is_removed_from_cli(self, tmp_path):
        """#1035: `--ci-command` はCLI引数から削除され、TOMLでのみ設定可能。CLIでの指定はエラー。"""
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                    "--ci-command",
                    "make ci",
                ],
                cwd=tmp_path,
            )
        assert excinfo.value.code == 2

    def test_ci_command_unset_defaults_to_none(self, tmp_path):
        """#394: `--ci-command`未指定時は`DispatcherConfig.ci_command`が
        `None`のままで、Integrator側の既定値フォールバックに委ねる（後方互換）。"""
        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
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
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
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
            ("parent-issue = 0\n", "parent issue cannot be set in configuration files"),
            ("run-state-path = 1\n", "must be a string path"),
        ],
    )
    def test_rejects_invalid_config_values(
        self, tmp_path, config, expected_error, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(config, encoding="utf-8")

        with pytest.raises(SystemExit) as error:
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

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
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        assert mock_build.call_args.args[0].dispatch_target_name == dispatch_target

    def test_post_cycle_failures_in_main(self, tmp_path, capsys, fake_forge):
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
        r5 = PhaseResult("post_finding_notices", PhaseStatus.SUCCESS)

        # ケース1: すべて成功
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=r1,
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=r2,
            ),
            patch(
                "orchestune.dispatch.dispatcher._process_parent_completion",
                autospec=True,
                return_value=r3,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=r4,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_finding_notices",
                autospec=True,
                return_value=r5,
            ),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )
            assert code == 0
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 5
            assert out["post_cycle_results"][0]["status"] == "success"

        # ケース2: RETRYABLE_FAILURE
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=r1,
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=r2_retryable,
            ),
            patch(
                "orchestune.dispatch.dispatcher._process_parent_completion",
                autospec=True,
                return_value=r3,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=r4,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_finding_notices",
                autospec=True,
                return_value=r5,
            ),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
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
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=r1,
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=r2_fatal,
            ),
            patch(
                "orchestune.dispatch.dispatcher._process_parent_completion",
                autospec=True,
                return_value=r3,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=r4,
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_finding_notices",
                autospec=True,
                return_value=r5,
            ),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "fatal_failure"

        # ケース4: main()のcheck_auth()自体がForgeAuthErrorを投げる場合
        fake_forge.check_auth.reset_mock(side_effect=True)
        fake_forge.check_auth.side_effect = ForgeAuthError("main-auth-failed")
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 5
            for res in out["post_cycle_results"]:
                assert res["status"] == "fatal_failure"
                assert "main-auth-failed" in res["error_message"]

    def test_post_event_log_comment_is_called_for_parent_issue(self, tmp_path):
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=PhaseResult(
                    "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=PhaseResult(
                    "run_semantic_integrator", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS),
            ) as mock_post,
        ):
            code = main(
                [
                    "--parent-issue",
                    "100",
                    "--apply",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        assert code == 0
        mock_post.assert_called_once()

    def test_post_event_log_comment_receives_cycle_report(self, tmp_path):
        """#396: `run_dispatch_cycle`が返した`CycleReport`が、そのまま
        `_post_event_log_comment`へ渡されること。"""
        cycle_report = self._empty_report()
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=cycle_report,
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=PhaseResult(
                    "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=PhaseResult(
                    "run_semantic_integrator", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._process_parent_completion",
                autospec=True,
                return_value=PhaseResult(
                    "process_parent_completion", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS),
            ) as mock_post,
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        assert code == 0
        mock_post.assert_called_once()
        assert mock_post.call_args.args[1] is cycle_report

    def test_post_finding_notices_receives_cycle_report(self, tmp_path):
        """#790: `run_dispatch_cycle`が返した`CycleReport`が、そのまま
        `_post_finding_notices`へ渡されること。"""
        cycle_report = self._empty_report()
        with (
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=cycle_report,
            ),
            patch(
                "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
                autospec=True,
                return_value=PhaseResult(
                    "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._run_semantic_integrator",
                autospec=True,
                return_value=PhaseResult(
                    "run_semantic_integrator", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._process_parent_completion",
                autospec=True,
                return_value=PhaseResult(
                    "process_parent_completion", PhaseStatus.SUCCESS
                ),
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_event_log_comment",
                autospec=True,
                return_value=PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS),
            ),
            patch(
                "orchestune.dispatch.dispatcher._post_finding_notices",
                autospec=True,
                return_value=PhaseResult("post_finding_notices", PhaseStatus.SUCCESS),
            ) as mock_post_findings,
        ):
            code = main(
                [
                    "--apply",
                    "--parent-issue",
                    "100",
                    "--allow-unsafe-agent-execution",
                ],
                cwd=tmp_path,
            )

        assert code == 0
        mock_post_findings.assert_called_once()
        assert mock_post_findings.call_args.args[1] is cycle_report

    def test_custom_window_seconds_preserves_launch_history_quota(
        self, tmp_path, fake_forge
    ):
        now = time.time()
        # window_seconds = 172800 (48時間)
        config = DispatcherConfig(
            parent_issue_number=181,
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
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
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
            main(
                ["--parent-issue", "100", "--dispatch-target", "claude-cli"],
                cwd=tmp_path,
            )
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid dispatcher config" in err
        assert "完全権限実行となります" in err

    def test_unsafe_cli_with_allow_unsafe_option_in_main_succeeds(self, tmp_path):
        with patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=self._empty_report(),
        ):
            code = main(
                [
                    "--parent-issue",
                    "100",
                    "--dispatch-target",
                    "claude-cli",
                    "--allow-unsafe-agent-execution",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )
            assert code == 0

    def test_unsafe_cli_with_allow_unsafe_option_in_orchestune_toml_succeeds(
        self, tmp_path, capsys
    ):
        """#1035: allow_unsafe_agent_execution はTOMLでは禁止され、CLIでのみ指定可能。"""
        orchestune_toml = tmp_path / "orchestune.toml"
        orchestune_toml.write_text(
            "allow_unsafe_agent_execution = true\n", encoding="utf-8"
        )
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--parent-issue",
                    "100",
                    "--dispatch-target",
                    "claude-cli",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )
        assert excinfo.value.code == 2
        assert (
            "setting 'allow_unsafe_agent_execution' is prohibited"
            in capsys.readouterr().err
        )

    def test_execution_profiles_loaded_from_orchestune_toml(self, tmp_path):
        """#668: orchestune.tomlからexecution_profilesとdefault_execution_profileがロードされること。"""
        orchestune_toml = tmp_path / "orchestune.toml"
        orchestune_toml.write_text(
            """
default_execution_profile = "deep"

[execution_profiles.deep.claude-cli]
model = "claude-3-7-sonnet-20250219"
reasoning_effort = "high"

[execution_profiles.fast.claude-cli]
model = "claude-3-5-haiku-20241022"
reasoning_effort = "low"
""",
            encoding="utf-8",
        )
        with (
            patch(
                "orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True
            ),
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                autospec=True,
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            code = main(
                [
                    "--parent-issue",
                    "100",
                    "--no-apply",
                ],
                cwd=tmp_path,
            )
            assert code == 0

        config_arg = mock_run.call_args.args[0]
        assert config_arg.execution_profile_config is not None
        assert config_arg.execution_profile_config.default_execution_profile == "deep"
        assert (
            config_arg.execution_profile_config.profiles["deep"]["claude-cli"].model
            == "claude-3-7-sonnet-20250219"
        )
        assert (
            config_arg.execution_profile_config.profiles["deep"][
                "claude-cli"
            ].reasoning_effort
            == "high"
        )

    def test_invalid_execution_profiles_in_toml_exits_with_error(
        self, tmp_path, capsys
    ):
        """#668: 不正なexecution_profiles設定がある場合はエラーで終了すること。"""
        orchestune_toml = tmp_path / "orchestune.toml"
        orchestune_toml.write_text(
            """
default_execution_profile = "deep"

[execution_profiles.deep.claude-cli]
model = "--dangerous-injected-flag"
""",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc_info:
            main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)

        assert exc_info.value.code == 2
        assert "invalid model name" in capsys.readouterr().err
