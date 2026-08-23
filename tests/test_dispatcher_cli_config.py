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
        assert mock_build.call_args.args[0].dispatch_target_name == "local"
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
        assert mock_build.call_args.args[0].dispatch_target_name == "claude-cli"
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
        fake_forge.check_auth.reset_mock(side_effect=True)
        fake_forge.check_auth.side_effect = ForgeAuthError("main-auth-failed")
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
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

    def test_custom_window_seconds_preserves_launch_history_quota(
        self, tmp_path, fake_forge
    ):
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
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
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
