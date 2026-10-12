"""Layer-table parsing: `pkg.*` wildcards and per-module exceptions (#1322)."""

from __future__ import annotations

from pathlib import Path

import pytest
from architecture_layer_table_support import (
    layer_table_tokens,
    parse_layer_table,
)

MODULES = {
    "alpha",
    "alpha.cli",
    "alpha.core",
    "alpha.sub.deep",
    "alpha_extra",
    "beta",
    "beta.contracts",
    "solo",
}
PACKAGES = {"alpha", "beta"}
EMPTY_ROWS = {4: "", 3: "", 2: "", 1: "", 0: ""}


def _table(rows: dict[int, str]) -> list[str]:
    merged = {**EMPTY_ROWS, **rows}
    lines = ["| Layer | Role | Modules |", "| --- | --- | --- |"]
    lines += [
        f"| **L{n}** | role | {merged[n]} |" for n in sorted(merged, reverse=True)
    ]
    return lines


def _parse(rows: dict[int, str]) -> dict[int, list[str]]:
    parsed: dict[int, list[str]] = parse_layer_table(_table(rows), MODULES, PACKAGES)
    return parsed


FULL = {
    4: "`alpha.cli`",
    2: "`alpha.*`, `solo`, `beta.*`",
    0: "`beta.contracts`",
}


class TestExpansion:
    def test_wildcard_expands_init_subpackages_and_exceptions(self) -> None:
        parsed = _parse({**FULL, 3: "", 1: ""})
        assert parsed[4] == ["alpha.cli"]
        assert parsed[2] == [
            "alpha",
            "alpha.core",
            "alpha.sub.deep",
            "beta",
            "solo",
        ]
        assert parsed[0] == ["beta.contracts"]

    def test_look_alike_package_name_is_not_swept_in(self) -> None:
        parsed = _parse({2: "`alpha.*`", 0: "`alpha_extra`"})
        assert "alpha_extra" in parsed[0]
        assert "alpha_extra" not in parsed[2]

    def test_row_order_does_not_change_the_result(self) -> None:
        lines = _table(FULL)
        assert parse_layer_table(lines, MODULES, PACKAGES) == parse_layer_table(
            list(reversed(lines)), MODULES, PACKAGES
        )

    def test_layer_comes_from_the_row_not_from_a_layer_map(self) -> None:
        in_l2 = _parse({2: "`alpha.*`"})
        in_l1 = _parse({1: "`alpha.*`"})
        assert "alpha.core" in in_l2[2] and "alpha.core" in in_l1[1]
        assert in_l2 != in_l1


class TestRejectedTables:
    def _reject(self, rows: dict[int, str], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            _parse(rows)

    def test_module_listed_twice_in_a_row(self) -> None:
        self._reject({2: "`solo`, `solo`"}, "twice in the L2 row")

    def test_module_listed_in_two_rows(self) -> None:
        self._reject({2: "`solo`", 0: "`solo`"}, "both L2 and L0")

    def test_wildcard_twice_in_a_row(self) -> None:
        self._reject(
            {2: "`alpha.*`, `alpha.*`"}, "wildcard `alpha.\\*` is listed twice"
        )

    def test_wildcard_in_two_rows(self) -> None:
        self._reject({2: "`alpha.*`", 0: "`alpha.*`"}, "both L2 and L0")

    @pytest.mark.parametrize("token", ["missing.*", "solo.*", "alpha.core.*"])
    def test_wildcard_must_name_a_real_top_level_package(self, token: str) -> None:
        with pytest.raises(ValueError):
            _parse({2: f"`{token}`"})

    def test_exception_in_the_wildcards_own_row_is_unnecessary(self) -> None:
        self._reject({2: "`alpha.*`, `alpha.core`"}, "unnecessary exception")

    def test_unknown_module(self) -> None:
        self._reject({2: "`alpha.nope`"}, "unknown module")

    def test_invalid_tokens_are_not_dropped(self) -> None:
        for token in ("alpha/core", "alpha.*.x", "1alpha", "*"):
            with pytest.raises(ValueError, match="invalid token"):
                _parse({2: f"`{token}`"})

    def test_text_outside_code_spans(self) -> None:
        self._reject({2: "`solo` and more"}, "outside code spans")

    def test_wildcard_with_nothing_left_to_expand(self) -> None:
        self._reject(
            {2: "`beta.*`", 0: "`beta`, `beta.contracts`"}, "unnecessary|expands to"
        )

    def test_only_l0_to_l4_rows_exist_exactly_once(self) -> None:
        with pytest.raises(ValueError, match="not one of L0-L4"):
            parse_layer_table(
                [*_table({}), "| **L5** | x | `solo` |"], MODULES, PACKAGES
            )
        with pytest.raises(ValueError, match="duplicate layer row L2"):
            parse_layer_table(
                [*_table({}), "| **L2** | x | `solo` |"], MODULES, PACKAGES
            )
        with pytest.raises(ValueError, match="missing layer rows"):
            parse_layer_table(_table({})[:-1], MODULES, PACKAGES)


def test_tokens_keep_wildcards_for_the_ja_en_comparison() -> None:
    tokens = layer_table_tokens(_table(FULL))
    assert tokens[2] == ["alpha.*", "solo", "beta.*"]


def test_a_wildcard_moved_to_the_wrong_row_fails_the_doc_sync_check() -> None:
    """The real table must not be expanded from `EXPECTED_LAYERS`' layers."""
    from architecture_layers_support import EXPECTED_LAYERS
    from architecture_test_support import _layer_table_universe

    lines = (
        (Path(__file__).parents[1] / "docs" / "en" / "architecture.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    moved = []
    for line in lines:
        if line.startswith("| **L2**"):
            assert "`claim.*`, " in line
            line = line.replace("`claim.*`, ", "", 1)
        elif line.startswith("| **L1**"):
            line = line.rstrip().rstrip("|").rstrip() + ", `claim.*` |"
        moved.append(line)
    modules, packages = _layer_table_universe()
    expected = {layer: sorted(mods) for layer, mods in EXPECTED_LAYERS.items()}
    assert parse_layer_table(moved, modules, packages) != expected
