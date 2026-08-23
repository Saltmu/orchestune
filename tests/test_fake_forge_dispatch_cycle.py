"""Regression guard for Issue #623 fake-forge migration."""

from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).parent
_MIGRATED_FILES = (
    "test_dispatch_cycle.py",
    "test_dispatch_cycle_completion.py",
    "test_dispatch_cycle_filters.py",
    "test_dispatch_cycle_locks.py",
    "test_dispatch_cycle_reconciliation_flow.py",
    "test_dispatch_cycle_reconciliation_recovery.py",
    "test_dispatch_cycle_worktree.py",
    "test_dispatcher_pipeline.py",
    "test_dispatcher_cli_config.py",
    "test_dispatcher_cli_options.py",
)


@pytest.mark.parametrize("filename", _MIGRATED_FILES)
def test_dispatch_cycle_tests_do_not_patch_github_forge_directly(filename: str) -> None:
    source = (_TESTS_ROOT / filename).read_text(encoding="utf-8")
    assert "orchestune.forge.GitHubForge" not in source
