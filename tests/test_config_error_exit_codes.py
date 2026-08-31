"""#428: 設定エラー（不正な`dag_ignore_patterns`）に対して、3ツール
（orchestune-dag/orchestune-provision/orchestune-dispatch）が同じ
exit code 2 で一貫して報告することの回帰防止テスト。

3ツールとも `orchestune/dag_models.py` の共有関数
（`load_orchestune_config`/`extract_dag_ignore_patterns`/
`compile_extra_ignore_patterns`）から `ConfigError` を送出させ、それぞれの
`main()` がこれを exit 2 にマッピングする。構造的エラー（`DagCycleError`等）
は引き続き exit 1 のままであることも合わせて確認する。
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dag.cli import main as dag_main
from orchestune.dispatch.dispatcher import main as dispatcher_main
from orchestune.provisioning.cli import main as provisioning_main

_PLAN = """\
---
title: "Example big rock"
subtasks:
  - id: task-a
    description: "Implement feature XX"
    footprint: [src/foo.py]
---

# Decomposition Plan
"""

_TEMPLATE = (
    "# [FEAT] {{subtask_id}}: {{description}}\n\n"
    "## Overview\n{{overview}}\n\n"
    "## Proposed Changes\n{{proposed_changes}}\n\n"
    "## Acceptance Criteria\n{{acceptance_criteria}}\n\n"
    "## Verification Plan\n{{verification_plan}}\n\n"
    "```yaml\n"
    "subtask_id: {{subtask_id_yaml}}\n"
    "footprint: {{footprint}}\n"
    "symbols: {{symbols}}\n"
    "depends_on: {{depends_on}}\n"
    "parent_issue_number: {{parent_issue_number}}\n"
    "execution_profile: {{execution_profile}}\n"
    "model_tier: {{model_tier}}\n"
    "```\n"
)

_INVALID_CONFIG = 'dag_ignore_patterns = ["("]\n'


def _write_invalid_orchestune_toml(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text(_INVALID_CONFIG, encoding="utf-8")


class TestConfigErrorExitCodeConsistency:
    def test_dag_cli_exits_2(self, tmp_path, capsys):
        _write_invalid_orchestune_toml(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2
        assert "Error:" in capsys.readouterr().err

    def test_provision_exits_2(self, tmp_path, capsys):
        _write_invalid_orchestune_toml(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2
        assert "Error:" in capsys.readouterr().err

    def test_dispatch_exits_2(self, tmp_path, capsys):
        _write_invalid_orchestune_toml(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
        assert "invalid dispatcher config" in capsys.readouterr().err

    def test_dag_cli_structural_error_still_exits_1(self, tmp_path, capsys):
        """設定エラーではない構造的エラー（DagCycleError）はexit 2に
        巻き込まれず、従来通りexit 1のままであることを確認する。"""
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(
            """\
---
subtasks:
  - id: task-a
    depends_on: ["task-b"]
  - id: task-b
    depends_on: ["task-a"]
---
""",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 1


class TestDispatcherConfigErrorNotAffectedByChange:
    """dispatcher.pyの既存の`except (ValueError, re.error)`は、`ConfigError`が
    `ValueError`のサブクラスであるため無改修のまま動作を維持することを
    念のため確認する（#428実装メモ）。"""

    def test_dispatcher_still_maps_invalid_regex_to_exit_2(self, tmp_path, capsys):
        _write_invalid_orchestune_toml(tmp_path)

        with (
            patch("orchestune.dispatch.dispatcher.build_dispatch_target"),
            pytest.raises(SystemExit) as excinfo,
        ):
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2


class TestNonTableToolSectionExitCodeConsistency:
    """Codex review (#441): a syntactically valid `pyproject.toml` whose
    top-level `tool` key isn't a table (e.g. `tool = "not-a-table"`) used to
    reach `data.get("tool", {}).get("orchestune", {})` and raise an uncaught
    `AttributeError` (`str` has no `.get`) instead of the intended
    `ConfigError`, so `orchestune-dispatch` crashed with exit 1 instead of
    the exit-2 config-error contract this PR introduces. Cover all three
    tools against the same malformed file."""

    _INVALID_TOOL_SECTION = 'tool = "not-a-table"\n'

    def test_dag_cli_exits_2(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            self._INVALID_TOOL_SECTION, encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2
        assert "[tool]" in capsys.readouterr().err

    def test_provision_exits_2(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            self._INVALID_TOOL_SECTION, encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2
        assert "[tool]" in capsys.readouterr().err

    def test_dispatch_exits_2(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            self._INVALID_TOOL_SECTION, encoding="utf-8"
        )

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
        assert "[tool]" in capsys.readouterr().err


class TestPathologicalRegexExitCodeConsistency:
    """Codex review (#441): `re.compile()` doesn't only fail with `re.error`
    for a malformed pattern. Two further exception types were found by
    Codex, each reproduced live and each initially left unwrapped so
    `orchestune-dispatch` crashed with an uncaught traceback (exit 1)
    instead of the exit-2 config-error contract this PR introduces:

    - An oversized repetition count (`a{9999999999999999999999999999999999999999}`)
      raises `OverflowError`.
    - ~500 nested groups (`"(" * 500 + "a" + ")" * 500`) raise
      `RecursionError`.

    `compile_extra_ignore_patterns` now catches broadly (`except Exception`)
    rather than chasing the regex compiler's exception types one by one;
    this parametrizes both known cases across all three tools as a
    regression guard.
    """

    _PATTERNS = {
        "oversized_repetition": "a{9999999999999999999999999999999999999999}",
        "deep_recursion": "(" * 500 + "a" + ")" * 500,
    }

    def _write_config(self, tmp_path: Path, pattern: str) -> None:
        (tmp_path / "orchestune.toml").write_text(
            f'dag_ignore_patterns = ["{pattern}"]\n', encoding="utf-8"
        )

    @pytest.mark.parametrize("pattern", _PATTERNS.values(), ids=_PATTERNS.keys())
    def test_dag_cli_exits_2(self, tmp_path, capsys, pattern):
        self._write_config(tmp_path, pattern)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2
        assert "dag_ignore_patterns" in capsys.readouterr().err

    @pytest.mark.parametrize("pattern", _PATTERNS.values(), ids=_PATTERNS.keys())
    def test_provision_exits_2(self, tmp_path, capsys, pattern):
        self._write_config(tmp_path, pattern)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2
        assert "dag_ignore_patterns" in capsys.readouterr().err

    @pytest.mark.parametrize("pattern", _PATTERNS.values(), ids=_PATTERNS.keys())
    def test_dispatch_exits_2(self, tmp_path, capsys, pattern):
        self._write_config(tmp_path, pattern)

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
        assert "dag_ignore_patterns" in capsys.readouterr().err


class TestInvalidUtf8ConfigExitCodeConsistency:
    """Codex review (#441): `orchestune.toml` containing invalid UTF-8 bytes
    makes `tomllib.load()` raise `UnicodeDecodeError`, which the original
    `except (OSError, tomllib.TOMLDecodeError)` didn't cover. Because
    `UnicodeDecodeError` is a `ValueError`, `orchestune-dispatch`'s own
    `except (ValueError, re.error)` happened to still map it to exit 2, but
    `orchestune-dag`/`orchestune-provision` reached their generic
    except-Exception handler and exited 1, breaking the three-tool
    consistency this PR introduces."""

    def _write_invalid_utf8_config(self, tmp_path: Path) -> None:
        (tmp_path / "orchestune.toml").write_bytes(b'key = "\xff\xfe invalid"\n')

    def test_dag_cli_exits_2(self, tmp_path, capsys):
        self._write_invalid_utf8_config(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2

    def test_provision_exits_2(self, tmp_path, capsys):
        self._write_invalid_utf8_config(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2

    def test_dispatch_exits_2(self, tmp_path, capsys):
        self._write_invalid_utf8_config(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2


class TestOversizedThresholdIntegerExitCodeConsistency:
    """Codex review (#441): `dag_similarity_threshold` as a TOML integer too
    large for `float` (tomllib accepts arbitrarily large integers) makes
    `float(value)` raise `OverflowError` before the `ConfigError` range
    check runs, so `orchestune-dispatch` crashed with an uncaught traceback
    (exit 1) instead of the exit-2 config-error contract this PR
    introduces."""

    _OVERSIZED_THRESHOLD_CONFIG = f"dag_similarity_threshold = {10**500}\n"

    def _write_config(self, tmp_path: Path) -> None:
        (tmp_path / "orchestune.toml").write_text(
            self._OVERSIZED_THRESHOLD_CONFIG, encoding="utf-8"
        )

    def test_dag_cli_exits_2(self, tmp_path, capsys):
        self._write_config(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2
        assert "dag_similarity_threshold" in capsys.readouterr().err

    def test_provision_exits_2(self, tmp_path, capsys):
        self._write_config(tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2
        assert "dag_similarity_threshold" in capsys.readouterr().err

    def test_dispatch_exits_2(self, tmp_path, capsys):
        self._write_config(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
        assert "dag_similarity_threshold" in capsys.readouterr().err


class TestDeeplyNestedTomlExitCodeConsistency:
    """Codex review (#441): a syntactically valid but deeply nested TOML
    value (`x = ` followed by ~500 nested arrays) makes `tomllib.load()`
    raise `RecursionError`, which `load_orchestune_config`'s original
    `except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError)` didn't
    cover, so `orchestune-dispatch` crashed with an uncaught traceback
    (exit 1) instead of the exit-2 config-error contract this PR
    introduces. Covers both the `orchestune.toml` and `pyproject.toml`
    code paths across all three tools."""

    _DEEPLY_NESTED_ARRAY = "x = " + "[" * 500 + "]" * 500 + "\n"

    def _write_orchestune_toml(self, tmp_path: Path) -> None:
        (tmp_path / "orchestune.toml").write_text(
            self._DEEPLY_NESTED_ARRAY, encoding="utf-8"
        )

    def _write_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\n" + self._DEEPLY_NESTED_ARRAY, encoding="utf-8"
        )

    @pytest.mark.parametrize(
        "write_config", [_write_orchestune_toml, _write_pyproject_toml]
    )
    def test_dag_cli_exits_2(self, tmp_path, capsys, write_config):
        write_config(self, tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            dag_main(["--plan", str(plan_path)])

        assert excinfo.value.code == 2

    @pytest.mark.parametrize(
        "write_config", [_write_orchestune_toml, _write_pyproject_toml]
    )
    def test_provision_exits_2(self, tmp_path, capsys, write_config):
        write_config(self, tmp_path)
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")
        template_path = tmp_path / "issue_template.md"
        template_path.write_text(_TEMPLATE, encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            provisioning_main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert excinfo.value.code == 2

    @pytest.mark.parametrize(
        "write_config", [_write_orchestune_toml, _write_pyproject_toml]
    )
    def test_dispatch_exits_2(self, tmp_path, capsys, write_config):
        write_config(self, tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            dispatcher_main(["--no-apply"], cwd=tmp_path)

        assert excinfo.value.code == 2
