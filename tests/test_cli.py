import sys
from unittest.mock import patch

import pytest


def test_cli_delegates_to_dag():
    from orchestune.cli import main

    test_args = ["orchestune", "dag", "--plan", "plan.md"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.dag.cli.main") as mock_dag_main,
    ):
        main()
        mock_dag_main.assert_called_once()
        assert sys.argv == ["orchestune", "--plan", "plan.md"]


def test_cli_delegates_to_dispatch():
    from orchestune.cli import main

    test_args = ["orchestune", "dispatch", "--apply"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.dispatch.dispatcher.main") as mock_dispatch_main,
    ):
        main()
        mock_dispatch_main.assert_called_once()
        assert sys.argv == ["orchestune", "--apply"]


def test_cli_delegates_to_bootstrap():
    from orchestune.cli import main

    test_args = ["orchestune", "bootstrap"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.bootstrap.main") as mock_bootstrap_main,
    ):
        main()
        mock_bootstrap_main.assert_called_once()
        assert sys.argv == ["orchestune"]


def test_cli_delegates_to_status():
    from orchestune.cli import main

    test_args = ["orchestune", "status", "--watch"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.monitor.main") as mock_monitor_main,
    ):
        main()
        mock_monitor_main.assert_called_once()
        assert sys.argv == ["orchestune", "--watch"]


def test_cli_setup_exits_0_on_success():
    from orchestune.cli import main

    test_args = ["orchestune", "setup"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.setup_skills.setup_skills", return_value=0),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0


def test_cli_setup_exits_1_when_setup_skills_fails():
    from orchestune.cli import main

    test_args = ["orchestune", "setup"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.setup_skills.setup_skills", return_value=1),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1


def test_cli_setup_with_workflow_skill_flag_passed_through():
    """#394: `--with-workflow-skill`フラグがsetup_skills()へ伝播すること。"""
    from orchestune.cli import main

    test_args = ["orchestune", "setup", "--with-workflow-skill"]
    with (
        patch("sys.argv", test_args),
        patch(
            "orchestune.setup_skills.setup_skills", return_value=0
        ) as mock_setup_skills,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    mock_setup_skills.assert_called_once_with(with_workflow_skill=True)


def test_cli_setup_without_flag_defaults_to_false():
    """#394: フラグ未指定時は`with_workflow_skill=False`で呼ばれる（後方互換）。"""
    from orchestune.cli import main

    test_args = ["orchestune", "setup"]
    with (
        patch("sys.argv", test_args),
        patch(
            "orchestune.setup_skills.setup_skills", return_value=0
        ) as mock_setup_skills,
        pytest.raises(SystemExit),
    ):
        main()

    mock_setup_skills.assert_called_once_with(with_workflow_skill=False)


def test_cli_no_args_exits(capsys):
    from orchestune.cli import main

    with patch("sys.argv", ["orchestune"]), pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Usage: orchestune <command>" in captured.out


def test_cli_invalid_command_exits(capsys):
    from orchestune.cli import main

    with (
        patch("sys.argv", ["orchestune", "invalid_cmd"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Unknown command: invalid_cmd" in captured.out
