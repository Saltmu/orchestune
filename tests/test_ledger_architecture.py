"""Package boundaries for the shared execution ledger."""

from test_architecture import PACKAGE_ROOT, _import_graph


def test_state_and_label_modules_are_owned_by_ledger() -> None:
    assert not (PACKAGE_ROOT / "dispatch" / "state.py").exists()
    assert not (PACKAGE_ROOT / "dispatch" / "labels.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "run_state.py").exists()
    assert (PACKAGE_ROOT / "ledger" / "status_labels.py").exists()


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
