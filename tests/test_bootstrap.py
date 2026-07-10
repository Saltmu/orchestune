from unittest.mock import MagicMock, patch

import pytest

from orchestune.bootstrap import main, run_bootstrap
from orchestune.forge import BootstrapResult, ForgeAuthError


def _fake_forge(*, auth_error: ForgeAuthError | None = None, result=None):
    forge = MagicMock()
    if auth_error is not None:
        forge.check_auth.side_effect = auth_error
    if result is not None:
        forge.ensure_labels.return_value = result
    return forge


class TestRunBootstrap:
    def test_returns_1_and_prints_error_on_auth_failure(self, capsys):
        forge = _fake_forge(auth_error=ForgeAuthError("boom"))

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "boom" in captured.err
        forge.ensure_labels.assert_not_called()

    def test_returns_0_and_prints_summary_on_success(self, capsys):
        forge = _fake_forge(
            result=BootstrapResult(created_labels=("a",), existing_labels=("b", "c"))
        )

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Labels created: 1" in captured.out
        assert "Labels already present: 2" in captured.out

    def test_uses_github_forge_by_default(self):
        with patch("orchestune.bootstrap.GitHubForge") as mock_forge_cls:
            mock_forge_cls.return_value = _fake_forge(
                result=BootstrapResult(created_labels=(), existing_labels=())
            )
            exit_code = run_bootstrap()

        mock_forge_cls.assert_called_once()
        assert exit_code == 0


class TestMain:
    def test_exits_with_run_bootstrap_return_code(self):
        with (
            patch("orchestune.bootstrap.run_bootstrap", return_value=1),
            patch("sys.argv", ["orchestune-bootstrap"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
