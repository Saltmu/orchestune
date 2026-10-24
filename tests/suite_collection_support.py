"""Support for pytest collection ordering and suite marker classification."""

from __future__ import annotations

from typing import Any

import pytest

SUITE_MARKERS = frozenset({"unit", "integration", "e2e"})

_HEAVY_TEST_MODULE_PRIORITY: dict[str, int] = {
    "tests/test_quint_dependency_replay.py": 0,
    "tests/test_dependency_liveness_stateful.py": 1,
    "tests/test_status_reconciliation_stateful.py": 2,
    "tests/test_status_events_stateful.py": 3,
    "tests/test_status_machine_stateful.py": 4,
    "tests/test_closed_loop.py": 5,
    "tests/test_setup_git_hooks.py": 6,
    "tests/test_verification_contracts.py": 7,
    "tests/test_architecture_forge_patch.py": 8,
    "tests/test_architecture.py": 9,
    "tests/test_footprint_paths.py": 10,
}


def _collection_priority_key(item: Any) -> int:
    module_path = item.nodeid.split("::", 1)[0]
    return _HEAVY_TEST_MODULE_PRIORITY.get(
        module_path, len(_HEAVY_TEST_MODULE_PRIORITY)
    )


def _suite_markers(item: Any) -> set[str]:
    return {
        marker.name for marker in item.iter_markers() if marker.name in SUITE_MARKERS
    }


def _classify_suite(item: Any) -> None:
    markers = _suite_markers(item)
    if len(markers) > 1:
        names = ", ".join(sorted(markers))
        raise pytest.UsageError(
            f"{item.nodeid} has multiple mutually exclusive suite markers: {names}"
        )
    if not markers:
        # Unit is the safe default. Tests crossing a real infrastructure or
        # public workflow boundary must opt into integration/e2e explicitly.
        item.add_marker(pytest.mark.unit)


def order_and_classify(items: list[Any]) -> None:
    """Give every collected test exactly one suite marker and front-load heavy modules."""
    for item in items:
        _classify_suite(item)
        markers = _suite_markers(item)
        if len(markers) != 1:  # pragma: no cover - defensive invariant
            raise pytest.UsageError(
                f"{item.nodeid} must have exactly one suite marker; got {markers}"
            )
    items.sort(key=_collection_priority_key)
