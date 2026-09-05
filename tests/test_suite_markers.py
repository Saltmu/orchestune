from types import SimpleNamespace

import pytest

from tests.conftest import SUITE_MARKERS, _classify_suite, _suite_markers


class _FakeItem:
    def __init__(self, *marker_names: str) -> None:
        self.nodeid = "tests/test_fake.py::test_example"
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
