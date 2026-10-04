"""Tests for safe atomic config storage, locking, backup, and conflict detection."""

from pathlib import Path

import pytest

from orchestune.config_wizard.document import ConfigDocument
from orchestune.config_wizard.storage import (
    ConfigConflictError,
    ConfigStorageError,
    save_config_document,
)


class TestConfigStorage:
    def test_save_init_success(self, tmp_path: Path):
        doc = ConfigDocument.load(tmp_path, mode="init")
        doc.set_value("ci-command", "pytest")
        content = doc.to_toml_string().encode("utf-8")

        receipt = save_config_document(
            doc.target_path, doc.snapshot, content, mode="init"
        )
        assert receipt.committed is True
        assert receipt.backup_path is None
        assert receipt.fsync_confirmed is True
        assert doc.target_path.read_bytes() == content

    def test_save_init_conflict_if_file_created_meanwhile(self, tmp_path: Path):
        doc = ConfigDocument.load(tmp_path, mode="init")
        doc.set_value("ci-command", "pytest")
        content = doc.to_toml_string().encode("utf-8")

        # Concurrent process creates the file before save
        doc.target_path.write_text("ci-command = 'concurrent'\n", encoding="utf-8")

        with pytest.raises(ConfigConflictError, match="created by another process"):
            save_config_document(doc.target_path, doc.snapshot, content, mode="init")

        # The concurrently created file must be untouched
        assert (
            doc.target_path.read_text(encoding="utf-8") == "ci-command = 'concurrent'\n"
        )

    def test_save_edit_success_with_backup(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        orig_content = b"ci-command = 'pytest'\n"
        target.write_bytes(orig_content)

        doc = ConfigDocument.load(tmp_path, mode="edit")
        doc.set_value("ci-command", "pytest -v")
        new_content = doc.to_toml_string().encode("utf-8")

        receipt = save_config_document(
            doc.target_path, doc.snapshot, new_content, mode="edit"
        )
        assert receipt.committed is True
        assert receipt.backup_path is not None
        assert receipt.backup_path.exists()
        assert receipt.backup_path.read_bytes() == orig_content
        assert target.read_bytes() == new_content

    def test_save_edit_conflict_if_file_modified(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        target.write_bytes(b"ci-command = 'pytest'\n")

        doc = ConfigDocument.load(tmp_path, mode="edit")
        doc.set_value("ci-command", "pytest -v")
        new_content = doc.to_toml_string().encode("utf-8")

        # Concurrent modification
        target.write_bytes(b"ci-command = 'modified'\n")

        with pytest.raises(ConfigConflictError, match="changed"):
            save_config_document(
                doc.target_path, doc.snapshot, new_content, mode="edit"
            )

        # Untouched
        assert target.read_bytes() == b"ci-command = 'modified'\n"

    def test_save_edit_conflict_if_source_pyproject_modified(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.orchestune]\nci-command = 'pytest'\n", encoding="utf-8"
        )

        doc = ConfigDocument.load(tmp_path, mode="init")
        new_content = b"ci-command = 'pytest -v'\n"

        # Concurrent modification to source pyproject.toml
        pyproject.write_text(
            "[tool.orchestune]\nci-command = 'modified'\n", encoding="utf-8"
        )

        with pytest.raises(ConfigConflictError, match="Source configuration file"):
            save_config_document(
                doc.target_path, doc.snapshot, new_content, mode="init"
            )

    def test_save_fails_with_storage_error_on_lock_contention(
        self, tmp_path: Path, monkeypatch
    ):
        doc = ConfigDocument.load(tmp_path, mode="init")
        doc.set_value("ci-command", "pytest")
        content = doc.to_toml_string().encode("utf-8")

        from orchestune.infra.process_utils import FileLock, FileLockContentionError

        def mock_enter(self):
            raise FileLockContentionError("simulated timeout")

        monkeypatch.setattr(FileLock, "__enter__", mock_enter)

        with pytest.raises(ConfigStorageError, match="ロック取得がタイムアウト"):
            save_config_document(doc.target_path, doc.snapshot, content, mode="init")
