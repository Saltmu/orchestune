import subprocess
from unittest.mock import patch

import pytest

from orchestune.dispatch import dispatcher
from orchestune.dispatch.dispatcher import main
from orchestune.infra.process_utils import platform_ci_script_relative


@pytest.fixture(autouse=True)
def repository(tmp_path, fake_forge):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)


def test_missing_ci_stops_before_reservation(tmp_path, capsys):
    with (
        patch.object(dispatcher, "reserve_report") as reserve,
        patch.object(dispatcher, "run_dispatch_cycle") as cycle,
    ):
        with pytest.raises(SystemExit) as error:
            main(
                ["--apply", "--parent-issue", "100", "--dispatch-target", "local"],
                cwd=tmp_path,
            )
    assert error.value.code == 2
    reserve.assert_not_called()
    cycle.assert_not_called()
    assert "ci-command" in capsys.readouterr().err


@pytest.mark.parametrize("configured", [False, True])
def test_ci_preflight_allows_script_or_config(tmp_path, configured):
    if configured:
        (tmp_path / "orchestune.toml").write_text('ci-command = "pytest"\n')
    else:
        script = tmp_path / platform_ci_script_relative()
        script.parent.mkdir()
        script.touch()
    with patch.object(dispatcher, "_execute_and_save", return_value=0) as execute:
        assert (
            main(
                ["--apply", "--parent-issue", "100", "--dispatch-target", "local"],
                cwd=tmp_path,
            )
            == 0
        )
    execute.assert_called_once()
