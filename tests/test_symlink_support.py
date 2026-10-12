"""Skip only the missing Windows symlink privilege, preserving other failures."""

from pathlib import Path

import pytest

from tests import symlink_support


@pytest.mark.parametrize("directory", [False, True])
def test_create_symlink_forwards_target_kind(monkeypatch, tmp_path, directory):
    calls = []
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda self, target, target_is_directory: calls.append(
            (self, target, target_is_directory)
        ),
    )
    link, target = tmp_path / "link", tmp_path / "target"
    symlink_support.create_test_symlink(link, target, target_is_directory=directory)
    assert calls == [(link, target, directory)]


@pytest.mark.parametrize(
    "windows,winerror,skip",
    [(True, 1314, True), (True, 5, False), (True, None, False), (False, 1314, False)],
)
def test_only_windows_missing_privilege_is_skipped(
    monkeypatch, tmp_path, windows, winerror, skip
):
    error = OSError("symlink creation failed")
    if winerror is not None:
        error.winerror = winerror
    monkeypatch.setattr(symlink_support, "WINDOWS", windows)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "symlink_to", fail)
    if skip:
        with pytest.raises(pytest.skip.Exception, match="WinError 1314"):
            symlink_support.create_test_symlink(tmp_path / "link", tmp_path / "target")
    else:
        with pytest.raises(OSError) as failure:
            symlink_support.create_test_symlink(tmp_path / "link", tmp_path / "target")
        assert failure.value is error
