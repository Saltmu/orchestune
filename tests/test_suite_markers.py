from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.suite_collection_support import (
    _HEAVY_TEST_MODULE_PRIORITY,
    SUITE_MARKERS,
    _classify_suite,
    _suite_markers,
    order_and_classify,
)


class _FakeItem:
    def __init__(
        self, *marker_names: str, nodeid: str = "tests/test_fake.py::test_example"
    ) -> None:
        self.nodeid = nodeid
        self._markers = [SimpleNamespace(name=name) for name in marker_names]

    def iter_markers(self):
        return iter(self._markers)

    def add_marker(self, marker) -> None:
        self._markers.append(SimpleNamespace(name=marker.name))


def test_suite_markers_are_registered_and_mutually_exclusive() -> None:
    assert SUITE_MARKERS == {"unit", "integration", "e2e"}


def test_unmarked_item_defaults_to_unit() -> None:
    item = _FakeItem()

    _classify_suite(item)

    assert _suite_markers(item) == {"unit"}


def test_multiple_suite_markers_are_rejected() -> None:
    item = _FakeItem("unit", "integration")

    with pytest.raises(pytest.UsageError, match="multiple mutually exclusive"):
        _classify_suite(item)


def test_heavy_test_module_priority_files_exist() -> None:
    repo_root = Path(__file__).parents[1]
    assert len(_HEAVY_TEST_MODULE_PRIORITY) == 11
    for rel_path in _HEAVY_TEST_MODULE_PRIORITY:
        assert (
            repo_root / rel_path
        ).is_file(), f"Priority entry {rel_path} does not exist"


def test_order_and_classify_front_loads_heavy_modules_and_preserves_order() -> None:
    item_unprioritized_1 = _FakeItem(nodeid="tests/test_alpha.py::test_a")
    item_heavy_replay_1 = _FakeItem(
        nodeid="tests/test_quint_dependency_replay.py::test_1"
    )
    item_heavy_replay_2 = _FakeItem(
        nodeid="tests/test_quint_dependency_replay.py::test_2"
    )
    item_unprioritized_2 = _FakeItem(nodeid="tests/test_beta.py::test_b")
    item_heavy_footprint = _FakeItem(nodeid="tests/test_footprint_paths.py::test_fp")
    item_heavy_closed_loop = _FakeItem(nodeid="tests/test_closed_loop.py::test_cl")

    items = [
        item_unprioritized_1,
        item_heavy_replay_1,
        item_heavy_replay_2,
        item_unprioritized_2,
        item_heavy_footprint,
        item_heavy_closed_loop,
    ]

    order_and_classify(items)

    assert items == [
        item_heavy_replay_1,
        item_heavy_replay_2,
        item_heavy_closed_loop,
        item_heavy_footprint,
        item_unprioritized_1,
        item_unprioritized_2,
    ]

    # Also verify that every item was given a suite marker (default unit)
    for item in items:
        assert _suite_markers(item) == {"unit"}
