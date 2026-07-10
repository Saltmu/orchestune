import sys
from unittest.mock import patch

import pytest


def test_cli_delegates_to_dag():
    from orchestune.cli import main

    test_args = ["orchestune", "dag", "--plan", "plan.md"]
    with patch("sys.argv", test_args), patch("orchestune.dag.main") as mock_dag_main:
        main()
        mock_dag_main.assert_called_once()
        assert sys.argv == ["orchestune", "--plan", "plan.md"]


def test_cli_delegates_to_dispatch():
    from orchestune.cli import main

    test_args = ["orchestune", "dispatch", "--apply"]
    with (
        patch("sys.argv", test_args),
        patch("orchestune.dispatcher.main") as mock_dispatch_main,
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
