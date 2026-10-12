"""Shared repository configuration discovery (`infra.repository_config`)."""

from __future__ import annotations

import pytest

from orchestune.config_contracts import ConfigError
from orchestune.infra.repository_config import (
    find_and_load_config_file,
    resolve_repo_root,
)


class TestFindAndLoadConfigFile:
    def test_missing_files_return_empty_dict(self, tmp_path):
        assert find_and_load_config_file(tmp_path) == {}

    def test_reads_orchestune_toml_top_level(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["a.json"]\nmax-concurrent = 5\n',
            encoding="utf-8",
        )
        config = find_and_load_config_file(tmp_path)
        assert config == {"dag_ignore_patterns": ["a.json"], "max-concurrent": 5}

    def test_falls_back_to_pyproject_toml_tool_orchestune_table(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\n" 'dag_ignore_patterns = ["a.json"]\n',
            encoding="utf-8",
        )
        config = find_and_load_config_file(tmp_path)
        assert config == {"dag_ignore_patterns": ["a.json"]}

    def test_orchestune_toml_takes_precedence_over_pyproject_toml(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["from-orchestune-toml"]\n', encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\n" 'dag_ignore_patterns = ["from-pyproject-toml"]\n',
            encoding="utf-8",
        )
        config = find_and_load_config_file(tmp_path)
        assert config == {"dag_ignore_patterns": ["from-orchestune-toml"]}

    def test_invalid_orchestune_toml_raises_value_error(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="orchestune.toml"):
            find_and_load_config_file(tmp_path)

    def test_non_table_tool_orchestune_section_raises_value_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool]\norchestune = 1\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="tool.orchestune"):
            find_and_load_config_file(tmp_path)

    def test_non_table_tool_section_raises_config_error_not_attribute_error(
        self, tmp_path
    ):
        """Codex review (#441): a syntactically valid `pyproject.toml` whose
        top-level `tool` key isn't a table (e.g. `tool = "not-a-table"`) must
        not reach `.get("orchestune", ...)` on a non-dict and blow up with an
        uncaught `AttributeError` — it should be reported as the same
        `ConfigError` as any other malformed config, so `orchestune-dag`/
        `orchestune-provision`/`orchestune-dispatch` all map it to exit 2
        rather than an uncaught traceback."""
        (tmp_path / "pyproject.toml").write_text(
            'tool = "not-a-table"\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match=r"\[tool\]"):
            find_and_load_config_file(tmp_path)


class TestPathInputs:
    def test_accepts_str_and_path_equivalently(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "max-concurrent = 3\n", encoding="utf-8"
        )
        assert find_and_load_config_file(str(tmp_path)) == {"max-concurrent": 3}
        assert find_and_load_config_file(tmp_path) == {"max-concurrent": 3}

    def test_invalid_utf8_is_config_error(self, tmp_path):
        (tmp_path / "orchestune.toml").write_bytes(b'key = "\xff\xfe"\n')
        with pytest.raises(ConfigError, match="failed to load"):
            find_and_load_config_file(tmp_path)

    def test_invalid_pyproject_toml_is_config_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("not valid [[[", encoding="utf-8")
        with pytest.raises(ConfigError, match="pyproject.toml"):
            find_and_load_config_file(tmp_path)


class TestResolveRepoRoot:
    def test_finds_git_directory_above_plan(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8"
        )
        plan = tmp_path / "docs" / "plan.md"
        plan.parent.mkdir()
        assert resolve_repo_root(plan) == tmp_path.resolve()

    def test_finds_git_file_for_linked_worktree(self, tmp_path):
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        assert resolve_repo_root(tmp_path / "plan.md") == tmp_path.resolve()

    def test_falls_back_to_plan_parent_outside_git(self, tmp_path):
        plan = tmp_path / "sub" / "plan.md"
        plan.parent.mkdir()
        assert resolve_repo_root(plan) == plan.parent.resolve()
