"""Package boundaries for the shared execution ledger."""

import pytest
import test_architecture as architecture
from architecture_test_support import PACKAGE_ROOT, _import_graph


@pytest.mark.parametrize(
    "source", ["ledger", "ledger.run_state", "ledger.nested.state"]
)
@pytest.mark.parametrize(
    "dependency", ["issue_parsing", "claim.service", "cli", "ledger_extra"]
)
def test_ledger_boundary_rejects_other_l2_and_higher_modules(
    monkeypatch: pytest.MonkeyPatch, source: str, dependency: str
) -> None:
    graph = {source: {dependency}}
    layers = {**architecture._module_layer(), "ledger_extra": 2}
    monkeypatch.setattr(architecture, "_import_graph", lambda: graph)
    monkeypatch.setattr(architecture, "_module_layer", lambda: layers)
    with pytest.raises(AssertionError, match=f"{source} -> {dependency}"):
        architecture.test_ledger_dependencies_stay_within_boundary()


def test_ledger_boundary_allows_internal_l0_and_l1_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = {
        "ledger": {"ledger.run_state"},
        "ledger.escalation": {"ledger.status_labels", "forge", "labels"},
        "ledger.run_state": {"ownership_contracts", "infra.process_utils"},
        "ledger_extra": {"claim.service"},
    }
    monkeypatch.setattr(architecture, "_import_graph", lambda: graph)
    architecture.test_ledger_dependencies_stay_within_boundary()


def test_state_and_label_modules_are_owned_by_ledger() -> None:
    assert not (PACKAGE_ROOT / "dispatch" / "state.py").exists()
    assert not (PACKAGE_ROOT / "dispatch" / "labels.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "run_state.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "status_labels.py").exists()


def test_active_worktree_modules_are_owned_by_ledger() -> None:
    assert (PACKAGE_ROOT / "ledger" / "active_records.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "active_codec.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "active_lifecycle.py").exists()
    assert not (PACKAGE_ROOT / "dispatch" / "active_records.py").exists()
    assert not (PACKAGE_ROOT / "claim" / "active_records.py").exists()


def test_active_worktree_modules_do_not_import_complete() -> None:
    graph = _import_graph()
    for mod in [
        "ledger.active_records",
        "ledger.active_codec",
        "ledger.active_lifecycle",
    ]:
        deps = graph.get(mod, set())
        assert not any(
            dep == "complete" or dep.startswith("complete.") for dep in deps
        ), f"{mod} imports complete: {deps}"


def test_ledger_does_not_import_claim_or_dispatch() -> None:
    ledger_modules = {
        name: dependencies
        for name, dependencies in _import_graph().items()
        if name == "ledger" or name.startswith("ledger.")
    }
    assert ledger_modules
    for dependencies in ledger_modules.values():
        assert not any(
            dependency.split(".")[0] in {"claim", "dispatch"}
            for dependency in dependencies
        )


def test_complete_does_not_import_dispatch() -> None:
    for name, dependencies in _import_graph().items():
        if name == "complete" or name.startswith("complete."):
            assert not any(
                dependency == "dispatch" or dependency.startswith("dispatch.")
                for dependency in dependencies
            ), name
