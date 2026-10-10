"""The contract table must be real, complete and equal to the documents (#1275).

``tests/verification_contracts.py`` is data.  This module checks it against

* a real pytest collection (parametrized cases included; skipped or xfailed
  tests never count as verification, except the pinned known-defect rows),
* the rule "verified needs a normal test and a control, unverified needs a reason",
* the contract ids that the test code actually raises (``require("<id>", ...)``),
* the ja/en ``verification-contracts.md`` tables.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.verification_contract_test_support import COLLECT_OUT_ENV
from tests.verification_contracts import CONTRACTS, Contract

ROOT = Path(__file__).resolve().parent.parent
_DOCS = ROOT / "docs"

#: Faults the Issue requires a control for (design section 4 of #1275).
REQUIRED_FAULTS = frozenset(
    {
        "remove-missing",
        "remove-before-add",
        "retry-noop",
        "fresh-guard-bypass",
        "hold-guard-bypass",
        "repair-disabled",
        "repair-delayed",
        "event-only",
        "budget-not-consumed",
        "persistent-budget-reset",
        "promotion-delayed",
        "promotion-suppressed",
        "stale-evidence",
        "intermediate-ignored",
        "reservation-guard-bypass",
        "empty-completion-set",
        "throwaway-context",
    }
)


def _referenced_ids(contract: Contract) -> list[str]:
    return [
        *contract.tests,
        *(node for nodes in contract.controls.values() for node in nodes),
    ]


@pytest.fixture(scope="module")
def collected(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, bool]]:
    """Real collection of every file the table names (this file excluded)."""
    files = sorted(
        {node.split("::")[0] for c in CONTRACTS for node in _referenced_ids(c)}
        - {"tests/test_verification_contracts.py"}
    )
    out = tmp_path_factory.mktemp("collect") / "nodes.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-n0",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.verification_contract_test_support",
            *files,
        ],
        cwd=ROOT,
        env={**os.environ, COLLECT_OUT_ENV: str(out)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    nodes: dict[str, dict[str, bool]] = json.loads(out.read_text(encoding="utf-8"))
    assert nodes, "the collection produced no test node"
    return nodes


def _matches(collected: dict[str, dict[str, bool]], node: str) -> list[str]:
    return [
        found
        for found in collected
        if found == node or found.startswith((node + "[", node + "::"))
    ]


def test_ids_are_unique_and_well_formed() -> None:
    ids = [c.id for c in CONTRACTS]
    assert len(ids) == len(set(ids))
    for contract in CONTRACTS:
        assert re.fullmatch(r"P[123][A-C]?-[A-Z0-9-]+", contract.id), contract.id
        assert contract.phase in {"1", "2", "3a", "3b", "3c"}


def test_every_referenced_node_exists_and_is_not_skipped(
    collected: dict[str, dict[str, bool]],
) -> None:
    for contract in CONTRACTS:
        for node in _referenced_ids(contract):
            found = _matches(collected, node)
            assert found, f"{contract.id}: no collected test matches {node}"
            for name in found:
                marks = collected[name]
                if contract.status == "known_defect" and node in contract.tests:
                    assert marks["xfail"], f"{name} must be a pinned (xfail) defect"
                    continue
                assert not marks["skip"], f"{contract.id}: {name} is skipped"
                assert not marks["xfail"], f"{contract.id}: {name} is xfailed"


def test_known_defects_pin_their_own_contract_id() -> None:
    """A strict xfail may only be backed by a violation of its own row's id."""
    for contract in CONTRACTS:
        if contract.status != "known_defect":
            continue
        for node in contract.tests:
            assert contract.id in _named_ids(
                node, "pinned_defect"
            ), f"{node} does not pin {contract.id} with pinned_defect(...)"


def test_status_rules() -> None:
    for contract in CONTRACTS:
        if contract.status == "verified":
            assert contract.tests, contract.id
            assert contract.controls, f"{contract.id}: verified needs a control"
            assert not contract.reason or contract.reason.strip()
        elif contract.status == "unverified":
            assert contract.reason, f"{contract.id}: unverified needs a reason"
            assert not contract.controls, f"{contract.id}: a control makes it verified"
        elif contract.status == "known_defect":
            # A pinned counterexample (strict xfail) is what turns the fix into an
            # XPASS failure that forces this row to be revisited.
            assert contract.tests, f"{contract.id}: known defect without a pinned test"
            assert contract.issues and contract.reason, contract.id
        else:
            assert contract.status == "out_of_scope"
            assert contract.reason and not contract.controls, contract.id


def test_every_required_fault_has_a_control() -> None:
    controlled = {fault for c in CONTRACTS for fault in c.controls}
    assert REQUIRED_FAULTS <= controlled, sorted(REQUIRED_FAULTS - controlled)


def test_contract_ids_match_the_assertions_in_the_tests() -> None:
    """Rows name ids that the code raises; the code raises no unlisted id."""
    raised: dict[str, set[str]] = {}
    pattern = re.compile(
        r'(?:require|expect_violation)\(\s*"(P[123][A-C]?-[A-Z0-9-]+)"'
    )
    for path in sorted((ROOT / "tests").glob("*.py")):
        if path.name in {"verification_contracts.py", "test_verification_contracts.py"}:
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            raised.setdefault(match.group(1), set()).add(path.name)
    table = {c.id for c in CONTRACTS}
    assert set(raised) <= table, f"unlisted contract ids: {sorted(set(raised) - table)}"
    for contract in CONTRACTS:
        if contract.status == "verified":
            assert contract.id in raised, f"{contract.id} is never asserted"
        for nodes in contract.controls.values():
            for node in nodes:
                assert contract.id in _named_ids(
                    node, "expect_violation"
                ), f"{node} never expects {contract.id} in expect_violation(...)"


def _named_ids(node: str, call_name: str) -> set[str]:
    """String constants inside ``call_name(...)`` calls of the referenced function."""
    path, *_, name = node.split("::")
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    functions = [
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == name.split("[")[0]
    ]
    assert functions, f"{node}: no function {name}"
    found: set[str] = set()
    for function in functions:
        for call in ast.walk(function):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == call_name
            ):
                found.update(
                    const.value
                    for arg in call.args
                    for const in ast.walk(arg)
                    if isinstance(const, ast.Constant) and isinstance(const.value, str)
                )
    return found


_ROW = re.compile(
    r"^\|\s*`(P[123][A-C]?-[A-Z0-9-]+)`\s*\|\s*([0-9abc]+)\s*\|\s*([a-z_]+)\s*\|"
    r"([^|\n]*)\|([^|\n]*)\|\s*$",
    re.M,
)


def _documented(
    lang: str,
) -> dict[str, tuple[str, str, frozenset[str], frozenset[int]]]:
    text = (_DOCS / lang / "verification-contracts.md").read_text(encoding="utf-8")
    rows = {}
    for cid, phase, status, faults, issues in _ROW.findall(text):
        rows[cid] = (
            phase,
            status,
            frozenset(re.findall(r"`([a-z-]+)`", faults)),
            frozenset(int(n) for n in re.findall(r"#(\d+)", issues)),
        )
    return rows


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_document_table_equals_the_contract_table(lang: str) -> None:
    documented = _documented(lang)
    expected = {
        c.id: (c.phase, c.status, frozenset(c.controls), frozenset(c.issues))
        for c in CONTRACTS
    }
    assert documented == expected


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_documents_state_the_scope_of_the_guarantee(lang: str) -> None:
    text = (_DOCS / lang / "verification-contracts.md").read_text(encoding="utf-8")
    for marker in (
        "<!-- contract-table -->",
        "<!-- cycle-definitions -->",
        "<!-- detection-matrix -->",
    ):
        assert marker in text, marker
    assert "unverified" in text and "known_defect" in text
