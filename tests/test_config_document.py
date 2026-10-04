"""Tests for ConfigDocument, tomlkit manipulation, and snapshot handling."""

from pathlib import Path

import pytest

from orchestune.config_wizard.document import (
    ConfigDocument,
    create_snapshot,
    verify_snapshot_consistency,
)
from orchestune.dag.models import ConfigError


class TestConfigSnapshot:
    def test_snapshot_non_existent_file(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        snap = create_snapshot(target)
        assert not snap.exists
        assert snap.raw_bytes is None
        assert snap.sha256 is None
        assert snap.is_symlink is False

        # Still consistent when file doesn't exist
        assert verify_snapshot_consistency(snap) == (True, "")

        # Inconsistent when file is created
        target.write_text("ci-command = 'pytest'\n", encoding="utf-8")
        is_consistent, reason = verify_snapshot_consistency(snap)
        assert not is_consistent
        assert "created" in reason.lower() or "exist" in reason.lower()

    def test_snapshot_existing_file(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        target.write_text("ci-command = 'pytest'\n", encoding="utf-8")
        snap = create_snapshot(target)
        assert snap.exists
        assert snap.raw_bytes is not None
        assert snap.sha256 is not None
        assert snap.is_symlink is False

        assert verify_snapshot_consistency(snap) == (True, "")

        # Modified content
        target.write_text("ci-command = 'ruff'\n", encoding="utf-8")
        is_consistent, reason = verify_snapshot_consistency(snap)
        assert not is_consistent

    def test_snapshot_symlink_rejected(self, tmp_path: Path):
        real_file = tmp_path / "real.toml"
        real_file.write_text("a = 1\n", encoding="utf-8")
        symlink_file = tmp_path / "link.toml"
        symlink_file.symlink_to(real_file)

        snap = create_snapshot(symlink_file)
        assert snap.is_symlink is True


class TestConfigDocumentLoading:
    def test_init_mode_with_existing_file_raises(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        target.write_text("ci-command = 'pytest'\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="already exists"):
            ConfigDocument.load(tmp_path, mode="init")

    def test_edit_mode_with_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="does not exist"):
            ConfigDocument.load(tmp_path, mode="edit")

    def test_edit_mode_preserves_comments_and_format(self, tmp_path: Path):
        content = (
            "# Top level comment\n"
            "dispatch-target = 'auto' # inline comment\n"
            "\n"
            "[execution_profiles.test]\n"
            "# Profile comment\n"
            "claude-cli = { model = 'claude-3-7-sonnet-20250219' }\n"
        )
        target = tmp_path / "orchestune.toml"
        target.write_text(content, encoding="utf-8")

        doc = ConfigDocument.load(tmp_path, mode="edit")
        assert doc.source_type == "orchestune.toml"
        assert doc.get_value("dispatch-target") == "auto"

        # Update a value
        doc.set_value("ci-command", "pytest -v")
        dumped = doc.to_toml_string()

        assert "# Top level comment" in dumped
        assert "# inline comment" in dumped
        assert "# Profile comment" in dumped
        assert 'ci-command = "pytest -v"' in dumped

    def test_init_mode_migrates_pyproject_tool_orchestune(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            "name = 'foo'\n"
            "\n"
            "[tool.orchestune]\n"
            "# Orchestune CI setting\n"
            "ci-command = 'pytest'\n"
            "max-concurrent = 3\n"
            "\n"
            "[tool.other]\n"
            "key = 'should not copy'\n",
            encoding="utf-8",
        )

        doc = ConfigDocument.load(tmp_path, mode="init")
        assert doc.source_type == "pyproject.toml"
        assert doc.get_value("ci-command") == "pytest"
        assert doc.get_value("max-concurrent") == 3

        dumped = doc.to_toml_string()
        assert "[tool.orchestune]" not in dumped
        assert "ci-command = 'pytest'" in dumped or 'ci-command = "pytest"' in dumped
        assert "# Orchestune CI setting" in dumped
        assert "should not copy" not in dumped

    def test_init_mode_empty_when_no_config(self, tmp_path: Path):
        doc = ConfigDocument.load(tmp_path, mode="init")
        assert doc.source_type == "empty"
        doc.set_value("dispatch-target", "local")
        dumped = doc.to_toml_string()
        assert 'dispatch-target = "local"' in dumped

    def test_corrupted_toml_raises_config_error(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        target.write_text("invalid = toml [[[\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="syntax error|invalid toml|parse"):
            ConfigDocument.load(tmp_path, mode="edit")

    def test_symlink_orchestune_toml_raises_config_error(self, tmp_path: Path):
        real_file = tmp_path / "real.toml"
        real_file.write_text("a = 1\n", encoding="utf-8")
        target = tmp_path / "orchestune.toml"
        target.symlink_to(real_file)
        with pytest.raises(ConfigError, match="symlink"):
            ConfigDocument.load(tmp_path, mode="edit")
