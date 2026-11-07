"""`orchestune reviewer resolve` reads checkout config and does not guess."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestune.cli import main as cli_main
from orchestune.dispatch.reviewer_cli import main

_AUTO_CODEX = """\
reviewer-bot = "auto"

[reviewer_by_target]
codex-cli = "codex"
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_auto_table_entry_prints_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", _AUTO_CODEX)

    code = main(["resolve", "--target", "codex-cli"], checkout_root=tmp_path)

    out, err = capsys.readouterr()
    assert code == 0
    assert out == "codex\n"
    assert err == ""


def test_explicit_reviewer_bot_beats_the_target_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "orchestune.toml",
        """\
reviewer-bot = "claude"

[reviewer_by_target]
codex-cli = "codex"
""",
    )

    code = main(["resolve", "--target", "codex-cli", "--json"], checkout_root=tmp_path)

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "config_source": "orchestune.toml",
        "reviewer": "claude",
        "source": "reviewer-bot",
        "target": "codex-cli",
    }


def test_unlisted_target_uses_the_builtin_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", _AUTO_CODEX)

    code = main(["resolve", "--target", "grok-cli", "--json"], checkout_root=tmp_path)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == "claude"
    assert payload["source"] == "builtin"


def test_local_without_an_entry_is_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", 'reviewer-bot = "auto"\n')

    code = main(["resolve", "--target", "local"], checkout_root=tmp_path)

    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert "local" in err
    assert "claude" not in out
    assert "codex" not in out


def test_local_table_entry_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "orchestune.toml",
        """\
reviewer-bot = "auto"

[reviewer_by_target]
local = "codex"
""",
    )

    code = main(["resolve", "--target", "local", "--json"], checkout_root=tmp_path)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == "codex"
    assert payload["source"] == "reviewer_by_target"


@pytest.mark.parametrize("override", ["claude", "codex", "skip"])
def test_override_beats_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: str
) -> None:
    _write(tmp_path / "orchestune.toml", _AUTO_CODEX)

    code = main(
        ["resolve", "--target", "local", "--override", override, "--json"],
        checkout_root=tmp_path,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == override
    assert payload["source"] == "override"
    assert payload["config_source"] == "orchestune.toml"


def test_hyphen_and_underscore_aliases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "orchestune.toml",
        """\
reviewer_bot = "auto"

[reviewer-by-target]
codex-cli = "codex"
""",
    )

    code = main(["resolve", "--target", "codex-cli", "--json"], checkout_root=tmp_path)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == "codex"
    assert payload["source"] == "reviewer_by_target"


def test_pyproject_table_is_used_when_orchestune_toml_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """\
[tool.orchestune]
reviewer-bot = "auto"

[tool.orchestune.reviewer_by_target]
codex-cli = "codex"
""",
    )

    code = main(["resolve", "--target", "codex-cli", "--json"], checkout_root=tmp_path)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == "codex"
    assert payload["config_source"] == "pyproject.toml"


def test_orchestune_toml_wins_over_pyproject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", 'reviewer-bot = "claude"\n')
    _write(
        tmp_path / "pyproject.toml",
        '[tool.orchestune]\nreviewer-bot = "codex"\n',
    )

    code = main(["resolve", "--target", "codex-cli"], checkout_root=tmp_path)

    assert code == 0
    assert capsys.readouterr().out == "claude\n"


def test_missing_config_uses_the_builtin_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["resolve", "--target", "codex-cli", "--json"], checkout_root=tmp_path)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "config_source": "none",
        "reviewer": "claude",
        "source": "builtin",
        "target": "codex-cli",
    }


def test_invalid_toml_exits_without_a_reviewer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", "reviewer-bot = [\n")

    code = main(["resolve", "--target", "codex-cli"], checkout_root=tmp_path)

    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert "orchestune reviewer:" in err


def test_invalid_reviewer_value_exits_without_a_reviewer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", 'reviewer-bot = "skip"\n')

    code = main(["resolve", "--target", "codex-cli"], checkout_root=tmp_path)

    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert "reviewer-bot" in err


def test_unknown_target_is_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", 'reviewer-bot = "auto"\n')

    code = main(["resolve", "--target", "not-a-target"], checkout_root=tmp_path)

    out, _err = capsys.readouterr()
    assert code == 1
    assert out == ""


def test_override_still_prints_when_config_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "orchestune.toml", "reviewer-bot = [\n")

    code = main(
        ["resolve", "--target", "codex-cli", "--override", "skip", "--json"],
        checkout_root=tmp_path,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewer"] == "skip"
    assert payload["source"] == "override"
    assert payload["config_source"] == "unreadable"


def test_resolve_help_lists_the_read_only_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["resolve", "--help"])

    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--target" in text
    assert "--override" in text
    assert "--json" in text
    assert "--apply" not in text


def test_target_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["resolve"])

    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_cli_help_delegates_to_reviewer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["orchestune", "reviewer", "resolve", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli_main()

    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--target" in text
    assert "--no-apply" not in text
