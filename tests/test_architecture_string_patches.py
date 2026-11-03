"""Architectural invariants verifying string patch counts and ratchet baseline."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
BASELINE_PATH = REPO_ROOT / ".orchestune" / "string-patch-baseline.json"


def _is_patch_call(node: ast.Call) -> bool:
    """Return True if node is patch(...) or obj.patch(...)."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "patch":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "patch":
        return True
    return False


def _is_monkeypatch_setattr_call(node: ast.Call) -> bool:
    """Return True if node is monkeypatch.setattr(...) with a string target."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "setattr":
        return True
    return False


def count_string_patches(
    test_paths: list[Path] | None = None,
) -> tuple[int, dict[str, int]]:
    """Count string patches in test files via AST.

    Counts:
    - patch("orchestune...", ...) including @patch(...) and mock.patch(...)
    - monkeypatch.setattr("orchestune...", ...)

    Excludes patch.object and object-target monkeypatch.setattr.
    Returns (total, {module_key: count}) where module_key is target.rsplit(".", 1)[0].
    """
    if test_paths is None:
        test_paths = sorted(TESTS_ROOT.rglob("*.py"))

    counts: Counter[str] = Counter()
    for path in test_paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if not (_is_patch_call(node) or _is_monkeypatch_setattr_call(node)):
                continue

            if not node.args:
                continue

            first_arg = node.args[0]
            if (
                isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, str)
                and first_arg.value.startswith("orchestune.")
            ):
                module_key = first_arg.value.rsplit(".", 1)[0]
                counts[module_key] += 1

    total = sum(counts.values())
    sorted_counts = {k: counts[k] for k in sorted(counts.keys())}
    return total, sorted_counts


def load_string_patch_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    """Load baseline configuration from JSON file."""
    if not path.exists():
        raise FileNotFoundError(f"Baseline file not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "counts" not in data or "total" not in data:
        raise ValueError(f"Invalid baseline format in {path}")
    return data


def verify_string_patch_counts(
    baseline: dict[str, Any],
    actual_total: int,
    actual_counts: dict[str, int],
) -> None:
    """Verify actual string patch counts against baseline.

    Fails if:
    - baseline total does not equal sum of baseline counts
    - count increases (new string patches are forbidden)
    - count decreases or a key disappears (prompts baseline update)
    - total mismatch
    """
    baseline_counts: dict[str, int] = baseline["counts"]
    baseline_total: int = baseline["total"]

    assert (
        baseline_total == sum(baseline_counts.values())
    ), f"Baseline total ({baseline_total}) does not match sum of counts ({sum(baseline_counts.values())})"

    # Check for increases
    increased: dict[str, tuple[int, int]] = {}
    for mod, actual in actual_counts.items():
        expected = baseline_counts.get(mod, 0)
        if actual > expected:
            increased[mod] = (expected, actual)

    assert not increased, (
        "String patch count increased! New string patches are not permitted. "
        f"Violations (expected <= actual): {increased}"
    )

    # Check for decreases or removed keys
    decreased: dict[str, tuple[int, int]] = {}
    for mod, expected in baseline_counts.items():
        actual = actual_counts.get(mod, 0)
        if actual < expected:
            decreased[mod] = (expected, actual)

    assert not decreased, (
        "String patch count decreased! Please update .orchestune/string-patch-baseline.json. "
        f"Reductions (baseline -> actual): {decreased}"
    )

    assert (
        actual_total == baseline_total
    ), f"Total string patch count mismatch: actual={actual_total}, baseline={baseline_total}"


class TestStringPatchRatchet:
    def test_baseline_matches_actual_counts(self) -> None:
        """String patch count must not exceed baseline, and baseline must be updated when counts decrease."""
        baseline = load_string_patch_baseline()
        actual_total, actual_counts = count_string_patches()
        verify_string_patch_counts(baseline, actual_total, actual_counts)

    def test_ratchet_fails_when_count_increases(self) -> None:
        baseline = {
            "version": 1,
            "total": 10,
            "counts": {"orchestune.foo": 10},
        }
        with pytest.raises(AssertionError, match="String patch count increased"):
            verify_string_patch_counts(
                baseline,
                actual_total=11,
                actual_counts={"orchestune.foo": 11},
            )

    def test_ratchet_fails_when_new_module_added(self) -> None:
        baseline = {
            "version": 1,
            "total": 10,
            "counts": {"orchestune.foo": 10},
        }
        with pytest.raises(AssertionError, match="String patch count increased"):
            verify_string_patch_counts(
                baseline,
                actual_total=11,
                actual_counts={"orchestune.foo": 10, "orchestune.bar": 1},
            )

    def test_ratchet_fails_when_count_decreases_to_prompt_update(self) -> None:
        baseline = {
            "version": 1,
            "total": 10,
            "counts": {"orchestune.foo": 10},
        }
        with pytest.raises(
            AssertionError, match="String patch count decreased! Please update"
        ):
            verify_string_patch_counts(
                baseline,
                actual_total=9,
                actual_counts={"orchestune.foo": 9},
            )

    def test_ratchet_fails_when_key_disappears_to_prompt_update(self) -> None:
        baseline = {
            "version": 1,
            "total": 10,
            "counts": {"orchestune.foo": 10},
        }
        with pytest.raises(
            AssertionError, match="String patch count decreased! Please update"
        ):
            verify_string_patch_counts(
                baseline,
                actual_total=0,
                actual_counts={},
            )

    def test_ratchet_fails_on_inconsistent_baseline_total(self) -> None:
        baseline = {
            "version": 1,
            "total": 15,
            "counts": {"orchestune.foo": 10},
        }
        with pytest.raises(AssertionError, match="Baseline total .* does not match"):
            verify_string_patch_counts(
                baseline,
                actual_total=10,
                actual_counts={"orchestune.foo": 10},
            )


class TestArchitectureDecoupling:
    def test_dispatch_gc_has_no_private_reexports(self) -> None:
        """orchestune.dispatch.gc.__all__ must not contain private names starting with '_'."""
        import orchestune.dispatch.gc as gc_mod

        private_all = [
            name for name in getattr(gc_mod, "__all__", []) if name.startswith("_")
        ]
        assert (
            not private_all
        ), f"Found private names in orchestune.dispatch.gc.__all__: {private_all}"

        docstring = gc_mod.__doc__ or ""
        assert (
            "backward-compatible imports" not in docstring
        ), "orchestune.dispatch.gc docstring still mentions backward-compatible imports"

    def test_pre_scheduling_integration_steps_not_duplicated(self) -> None:
        """_pre_scheduling_integration_steps must only be defined in pre_scheduling_integration.py."""
        dispatcher_path = REPO_ROOT / "orchestune" / "dispatch" / "dispatcher.py"
        psi_path = (
            REPO_ROOT / "orchestune" / "dispatch" / "pre_scheduling_integration.py"
        )

        dispatcher_tree = ast.parse(dispatcher_path.read_text(encoding="utf-8"))
        psi_tree = ast.parse(psi_path.read_text(encoding="utf-8"))

        dispatcher_fns = {
            node.name
            for node in ast.walk(dispatcher_tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        psi_fns = {
            node.name
            for node in ast.walk(psi_tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }

        assert (
            "_pre_scheduling_integration_steps" in psi_fns
        ), "_pre_scheduling_integration_steps must be defined in pre_scheduling_integration.py"
        assert (
            "_pre_scheduling_integration_steps" not in dispatcher_fns
        ), "_pre_scheduling_integration_steps is still duplicated in dispatcher.py"

        # Check dispatcher.py does not import the 3 unused helpers
        forbidden_imports = {
            "_poll_pending_not_needed_reviews",
            "_advance_pending_semantic_reviews",
            "_run_semantic_integrator",
        }
        dispatcher_imports: set[str] = set()
        for node in ast.walk(dispatcher_tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    dispatcher_imports.add(alias.name)
        leaked = forbidden_imports & dispatcher_imports
        assert not leaked, f"dispatcher.py still imports unused helpers: {leaked}"


class TestStringPatchCounterUnit:
    def test_counter_recognizes_various_patch_forms(self, tmp_path: Path) -> None:
        code = """
from unittest.mock import patch
import pytest

@patch("orchestune.dispatch.dispatcher.run")
def test_foo():
    with patch(
        "orchestune.dispatch.dispatcher.other",
        return_value=1
    ):
        pass

def test_bar(monkeypatch):
    monkeypatch.setattr("orchestune.dispatch.gc.completion.flag", True)
    monkeypatch.setattr(some_obj, "attr", 123)

@patch.object(some_obj, "attr")
def test_baz():
    pass
"""
        f = tmp_path / "test_sample.py"
        f.write_text(code, encoding="utf-8")

        total, counts = count_string_patches([f])
        assert total == 3
        assert counts == {
            "orchestune.dispatch.dispatcher": 2,
            "orchestune.dispatch.gc.completion": 1,
        }
