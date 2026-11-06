import json

import pytest

from orchestune.config_wizard.ci_candidates import suggest_ci_commands
from orchestune.infra.process_utils import platform_ci_script_relative


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("Makefile", "ci:\n\tfalse\ntest:\n", ["make ci", "make test"]),
        ("Makefile", ".PHONY: ci test\n", ["make ci", "make test"]),
        ("Makefile", "#ci:\n\tci:\n", []),
        (
            "package.json",
            json.dumps({"scripts": {"ci": "check", "test": "test"}}),
            ["npm run ci", "npm run test"],
        ),
        ("package.json", "{broken", []),
        ("pyproject.toml", "[tool.pytest.ini_options]\n", ["pytest"]),
        ("pyproject.toml", "[tool.pytest]\n", ["pytest"]),
        ("pyproject.toml", "[broken", []),
    ],
)
def test_candidates(tmp_path, filename, content, expected):
    (tmp_path / filename).write_text(content)
    assert suggest_ci_commands(tmp_path) == expected


def test_script_first(tmp_path):
    import shlex

    from orchestune.infra.process_utils import default_ci_command

    script = tmp_path / platform_ci_script_relative()
    script.parent.mkdir()
    script.touch()
    (tmp_path / "Makefile").write_text("ci:\n")
    assert suggest_ci_commands(tmp_path) == [
        shlex.join(default_ci_command()),
        "make ci",
    ]
