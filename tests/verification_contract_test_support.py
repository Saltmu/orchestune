"""Contract-id violations and the control (fault-detection) helper (#1275).

A guarantee's assertion raises ``ContractViolation`` carrying the contract id
from ``tests/verification_contracts.py``.  A control test injects one fault with
``monkeypatch`` and runs the *same* deterministic scenario as the normal test;
only a violation of the **expected** contract id counts as detection.  A
different contract, an unrelated exception, or no failure at all fails the
control, so a broken premise can never be counted as "detected".

The module doubles as a pytest plugin (``-p tests.verification_contract_test_support``):
``tests/test_verification_contracts.py`` runs a real collection with it to learn
which node ids exist and whether they carry a skip/xfail mark.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: Environment variable naming the JSON file the collection plugin writes.
COLLECT_OUT_ENV = "ORCHESTUNE_CONTRACT_COLLECT_OUT"


class ContractViolation(AssertionError):
    """A verification contract's assertion failed (``contract_id`` says which)."""

    def __init__(self, contract_id: str, detail: str = "") -> None:
        self.contract_id = contract_id
        self.detail = detail
        super().__init__(f"[{contract_id}] {detail}".rstrip())


def require(contract_id: str, condition: object, detail: object = "") -> None:
    """Assert ``condition`` for ``contract_id``; the violation names the contract."""
    if not condition:
        raise ContractViolation(contract_id, str(detail))


@contextmanager
def expect_violation(contract_id: str) -> Iterator[None]:
    """Succeed only when the body fails with a violation of ``contract_id``.

    The body must have reached its assertions: precondition failures, other
    contracts' violations and unrelated exceptions are reported as errors, never
    as a detection.
    """
    try:
        yield
    except ContractViolation as violation:
        if violation.contract_id != contract_id:
            raise AssertionError(
                f"expected a violation of {contract_id}, got {violation}"
            ) from violation
        return
    raise AssertionError(f"the injected fault did not violate {contract_id}")


# ---- collection plugin ----------------------------------------------------------


def pytest_collection_finish(session: Any) -> None:
    """Write ``{node id: {"skip": bool, "xfail": bool}}`` for the collected items."""
    target = os.environ.get(COLLECT_OUT_ENV)
    if not target:
        return
    nodes: dict[str, dict[str, bool]] = {}
    for item in session.items:
        names = {mark.name for mark in item.iter_markers()}
        nodes[item.nodeid] = {
            "skip": bool(names & {"skip", "skipif"}),
            "xfail": "xfail" in names,
        }
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(nodes, handle)
