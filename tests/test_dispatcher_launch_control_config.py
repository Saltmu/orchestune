"""#1154: 起動制御の既定値（並行数主軸）と不正値の起動時検証。"""

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.dispatcher import main
from tests.dispatch_test_support import (
    stub_forge_check_auth,
    stub_label_actor_permission,
)


@pytest.fixture(autouse=True)
def _stub_github_auth(fake_forge):
    stub_forge_check_auth(fake_forge)
    stub_label_actor_permission(fake_forge)


@pytest.fixture(autouse=True)
def _resolve_temp_cwds(monkeypatch):
    """設定ファイルだけを一時ディレクトリに置き、ワークスペース解決は本リポジトリで行う。"""
    from orchestune.dispatch import dispatcher

    resolve = dispatcher._resolve_dispatch_shared_paths
    repository_cwd = Path.cwd()

    def _resolve(args, cwd):
        return resolve(args, repository_cwd)

    monkeypatch.setattr(dispatcher, "_resolve_dispatch_shared_paths", _resolve)


def _empty_report():
    return CycleReport(
        selected=[],
        quota_slots_available=0,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=False,
    )


def _run_with_toml(tmp_path, toml_text=None):
    if toml_text is not None:
        (tmp_path / "orchestune.toml").write_text(toml_text, encoding="utf-8")
    with (
        patch("orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=_empty_report(),
        ) as mock_run,
    ):
        main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)
    return mock_run.call_args.args[0]


def test_launch_control_defaults_are_concurrency_primary(tmp_path):
    """#1154: 起動数上限なし・window=2時間・timeout=2時間が新しい既定値。"""
    config = _run_with_toml(tmp_path)
    assert config.max_launches_per_window is None
    assert config.window_seconds == 7200
    assert config.task_timeout_seconds == 7200
    assert config.max_concurrent == 2


def test_launch_control_explicit_values_are_preserved(tmp_path):
    config = _run_with_toml(
        tmp_path,
        "max-launches-per-window = 0\n"
        "window-seconds = 3600\n"
        "task-timeout-seconds = 0\n",
    )
    assert config.max_launches_per_window == 0
    assert config.window_seconds == 3600
    assert config.task_timeout_seconds == 0


def test_launch_limit_with_omitted_window_uses_two_hour_window(tmp_path):
    config = _run_with_toml(tmp_path, "max-launches-per-window = 1\n")
    assert config.max_launches_per_window == 1
    assert config.window_seconds == 7200


@pytest.mark.parametrize(
    "line",
    [
        "max-launches-per-window = -1",
        "max-launches-per-window = true",
        "max-launches-per-window = 1.5",
        'max-launches-per-window = "1"',
        "window-seconds = 0",
        "task-timeout-seconds = -1",
    ],
)
def test_invalid_launch_control_values_are_rejected(tmp_path, line):
    (tmp_path / "orchestune.toml").write_text(line + "\n", encoding="utf-8")
    with (
        patch("orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=_empty_report(),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--parent-issue", "100", "--no-apply"], cwd=tmp_path)
    assert excinfo.value.code == 2
