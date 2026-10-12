"""Parsing of the architecture documents' layer table (§4.1) into per-layer modules.

A cell lists two kinds of code spans:

* ``name`` -- exactly that module (a bare package name is its ``__init__``);
* ``pkg.*`` -- every module of top-level package ``pkg`` that no span names
  individually, ``pkg`` itself and nested subpackages included.

The layer of an expanded module comes from the row its span sits in, never from
``EXPECTED_LAYERS``, so a wrongly placed wildcard fails the doc-sync test.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable

LAYERS = (4, 3, 2, 1, 0)

_LAYER_ROW = re.compile(r"^\s*\|\s*\*\*L(\d+)\*\*")
_CODE_SPAN = re.compile(r"`([^`]*)`")
_MODULE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_WILDCARD_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.\*$")


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def _last_cell(line: str) -> str:
    return line.strip().strip("|").split("|")[-1].strip()


def layer_table_tokens(lines: Iterable[str]) -> dict[int, list[str]]:
    """The raw code spans of each `**L<n>**` row, in document order.

    Rejects rows outside L0-L4, a missing or repeated layer row, text between
    the spans other than commas, and any span that is neither a dotted module
    name nor a `pkg.*` wildcard -- nothing is silently dropped.
    """
    tokens: dict[int, list[str]] = {}
    for line in lines:
        match = _LAYER_ROW.match(line)
        if match is None:
            continue
        layer = int(match.group(1))
        if layer not in LAYERS:
            raise ValueError(f"layer row L{layer} is not one of L0-L4")
        if layer in tokens:
            raise ValueError(f"duplicate layer row L{layer}")
        cell = _last_cell(line)
        spans = _CODE_SPAN.findall(cell)
        leftover = _CODE_SPAN.sub("", cell).replace(",", "").strip()
        if leftover:
            raise ValueError(f"L{layer} row has text outside code spans: {leftover!r}")
        for span in spans:
            if not (_MODULE_TOKEN.match(span) or _WILDCARD_TOKEN.match(span)):
                raise ValueError(f"L{layer} row has an invalid token: `{span}`")
        tokens[layer] = spans
    missing = [layer for layer in LAYERS if layer not in tokens]
    if missing:
        raise ValueError(f"missing layer rows: {['L%d' % layer for layer in missing]}")
    return tokens


def _where(first: int, second: int) -> str:
    return (
        f"twice in the L{second} row"
        if first == second
        else f"in both L{first} and L{second}"
    )


def _individual_layers(tokens: dict[int, list[str]], known: set[str]) -> dict[str, int]:
    individual: dict[str, int] = {}
    for layer, row in tokens.items():
        for token in row:
            if _WILDCARD_TOKEN.match(token):
                continue
            if token not in known:
                raise ValueError(f"L{layer} row names an unknown module: `{token}`")
            if token in individual:
                raise ValueError(
                    f"module `{token}` is listed {_where(individual[token], layer)}"
                )
            individual[token] = layer
    return individual


def _wildcard_layers(
    tokens: dict[int, list[str]], packages: Collection[str]
) -> dict[str, int]:
    wildcard_layer: dict[str, int] = {}
    for layer, row in tokens.items():
        for token in row:
            wildcard = _WILDCARD_TOKEN.match(token)
            if wildcard is None:
                continue
            package = wildcard.group(1)
            if package not in packages:
                raise ValueError(
                    f"`{token}` must name a top-level package with an __init__.py"
                )
            if package in wildcard_layer:
                raise ValueError(
                    f"wildcard `{package}.*` is listed "
                    f"{_where(wildcard_layer[package], layer)}"
                )
            wildcard_layer[package] = layer
    return wildcard_layer


def expand_layer_tokens(
    tokens: dict[int, list[str]],
    modules: Collection[str],
    packages: Collection[str],
) -> dict[int, list[str]]:
    """Expand individual names and `pkg.*` wildcards into {layer: modules}.

    `modules` are the real modules (without the `orchestune.` prefix and
    without the package root); `packages` are the top-level names backed by an
    `__init__.py`. Duplicates are rejected before anything is collapsed.
    """
    known = set(modules)
    individual = _individual_layers(tokens, known)
    wildcard_layer = _wildcard_layers(tokens, packages)

    for module, layer in individual.items():
        if wildcard_layer.get(_top_level(module)) == layer:
            raise ValueError(
                f"`{module}` is an unnecessary exception: `{_top_level(module)}.*` "
                f"already puts it in L{layer}"
            )

    expanded: dict[int, list[str]] = {layer: [] for layer in tokens}
    for module, layer in individual.items():
        expanded[layer].append(module)
    for package, layer in wildcard_layer.items():
        members = [
            module
            for module in known
            if (module == package or module.startswith(f"{package}."))
            and module not in individual
        ]
        if not members:
            raise ValueError(
                f"wildcard `{package}.*` expands to nothing: every module is listed"
            )
        expanded[layer].extend(members)
    _require_majority_wildcards(expanded, wildcard_layer)
    return {layer: sorted(row) for layer, row in expanded.items()}


def _require_majority_wildcards(
    expanded: dict[int, list[str]], wildcard_layer: dict[str, int]
) -> None:
    """`pkg.*` belongs to the layer holding most of the package's modules.

    Without a unique majority the package has no default layer, so every module
    must be listed individually instead.
    """
    for package, layer in wildcard_layer.items():
        counts = Counter(
            module_layer
            for module_layer, row in expanded.items()
            for module in row
            if _top_level(module) == package
        )
        ranked = counts.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise ValueError(
                f"`{package}.*` has no unique majority layer: list every module of "
                f"`{package}` individually"
            )
        if ranked[0][0] != layer:
            raise ValueError(
                f"`{package}.*` sits in L{layer} but most of `{package}` is in "
                f"L{ranked[0][0]}"
            )


def parse_layer_table(
    lines: Iterable[str], modules: Collection[str], packages: Collection[str]
) -> dict[int, list[str]]:
    """The documented {layer: modules} map with wildcards expanded."""
    return expand_layer_tokens(layer_table_tokens(lines), modules, packages)
