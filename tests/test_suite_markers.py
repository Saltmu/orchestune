from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.suite_collection_support import (
    _AST_SCAN_TEST_MODULES,
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
        self._markers = [
            SimpleNamespace(name=name, args=(), kwargs={}) for name in marker_names
        ]

    def iter_markers(self, name: str | None = None):
        if name is not None:
            return iter(m for m in self._markers if m.name == name)
        return iter(self._markers)

    def add_marker(self, marker) -> None:
        args = getattr(marker, "args", ())
        kwargs = getattr(marker, "kwargs", {})
        self._markers.append(
            SimpleNamespace(name=marker.name, args=args, kwargs=kwargs)
        )


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
    assert len(_HEAVY_TEST_MODULE_PRIORITY) == 16
    for rel_path in _HEAVY_TEST_MODULE_PRIORITY:
        assert (
            repo_root / rel_path
        ).is_file(), f"Priority entry {rel_path} does not exist"


def test_architecture_source_support_priority_precedes_forge_patch() -> None:
    source_priority = _HEAVY_TEST_MODULE_PRIORITY[
        "tests/test_architecture_source_support.py"
    ]
    forge_priority = _HEAVY_TEST_MODULE_PRIORITY[
        "tests/test_architecture_forge_patch.py"
    ]
    assert source_priority < forge_priority


def test_ast_scan_test_modules_files_exist() -> None:
    repo_root = Path(__file__).parents[1]
    assert len(_AST_SCAN_TEST_MODULES) == 11
    assert "tests/test_labels.py" in _AST_SCAN_TEST_MODULES
    assert "tests/test_architecture_source_support.py" in _AST_SCAN_TEST_MODULES
    for rel_path in _AST_SCAN_TEST_MODULES:
        assert (
            repo_root / rel_path
        ).is_file(), f"AST scan module {rel_path} does not exist"


def test_order_and_classify_assigns_ast_scan_xdist_group() -> None:
    item_ast = _FakeItem(nodeid="tests/test_labels.py::test_no_direct_status")
    item_other = _FakeItem(nodeid="tests/test_fake.py::test_example")

    order_and_classify([item_ast, item_other])

    ast_markers = list(item_ast.iter_markers("xdist_group"))
    assert len(ast_markers) == 1
    assert ast_markers[0].args == ("ast_scan",)

    other_markers = list(item_other.iter_markers("xdist_group"))
    assert len(other_markers) == 0


def test_pytest_collection_modifyitems_is_tryfirst() -> None:
    from tests.conftest import pytest_collection_modifyitems

    pytest_impl = getattr(pytest_collection_modifyitems, "pytest_impl", None)
    assert (
        pytest_impl is not None
    ), "pytest_collection_modifyitems must be decorated with @pytest.hookimpl"
    assert pytest_impl.get("tryfirst") is True


def test_order_and_classify_front_loads_packaging_and_subprocess_modules() -> None:
    item_unprioritized = _FakeItem(nodeid="tests/test_zeta.py::test_z")
    item_wheel = _FakeItem(nodeid="tests/test_wheel_skills_packaging.py::test_build")
    item_git_ops = _FakeItem(nodeid="tests/test_integrator_git_ops.py::test_ops")
    item_typing = _FakeItem(
        nodeid="tests/test_dispatch_cycle_execution_typing.py::test_typing"
    )
    item_ports = _FakeItem(
        nodeid="tests/test_dispatch_cycle_port_contracts.py::test_ports"
    )

    items = [
        item_unprioritized,
        item_wheel,
        item_git_ops,
        item_typing,
        item_ports,
    ]
    order_and_classify(items)

    assert items == [
        item_wheel,
        item_git_ops,
        item_typing,
        item_ports,
        item_unprioritized,
    ]


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
