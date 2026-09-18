"""Mechanical contracts for the bilingual dependency architecture documents.

The contracts here are deliberately narrow. Each #875 contract is checked inside
the anchor section that owns it, and the fallback rows are compared against
meanings defined as constants in this file rather than re-derived from the
documents. What this file does *not* do is assert a full-text snapshot or equal
character counts between the two languages: translation equivalence is a
human diff-review concern, and no test here should be read as guaranteeing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orchestune.dispatch.rules import CycleContext

REPO_ROOT = Path(__file__).parents[1]

DOCUMENTS = {
    language: {
        "overview": REPO_ROOT / f"docs/{language}/architecture.md",
        "dag": REPO_ROOT / f"docs/{language}/architecture/dag-and-scheduling.md",
        "state": REPO_ROOT / f"docs/{language}/architecture/state-recovery.md",
        "integration": REPO_ROOT / f"docs/{language}/architecture/integration.md",
    }
    for language in ("en", "ja")
}

ANCHORS = {
    "overview": ("dependency-cycle-context",),
    "dag": (
        "dependency-three-layers",
        "dependency-stack-contract",
        "dependency-ordering",
    ),
    "state": (
        "dependency-record-postconditions",
        "dependency-fresh-validation",
    ),
    "integration": ("dependency-target-fallback",),
}


@dataclass(frozen=True)
class _SectionContract:
    """Phrases that must appear inside one anchor section.

    `shared` holds language-neutral spellings (API, enum and field names, and
    stable English terms kept untranslated). `ja` / `en` hold the prose that
    carries the same contract in each language.
    """

    document: str
    anchor: str
    shared: tuple[str, ...] = ()
    ja: tuple[str, ...] = ()
    en: tuple[str, ...] = ()

    def expected(self, language: str) -> tuple[str, ...]:
        return self.shared + (self.ja if language == "ja" else self.en)


SECTION_CONTRACTS = (
    _SectionContract(
        document="dag",
        anchor="dependency-three-layers",
        shared=(
            "Identity Resolution",
            "Lifecycle Assessment",
            "Use-case Policy",
            "COMPLETED > CHANGES_REQUESTED > CI_PASSED_UNMERGED > WAITING",
            "NOT_NEEDED",
            "subtask_id",
        ),
    ),
    _SectionContract(
        document="dag",
        anchor="dependency-stack-contract",
        shared=("A depends on B", "CI_PASSED_UNMERGED", "COMPLETED"),
    ),
    # #911: one phrase per ordering guarantee, so deleting any single guarantee
    # fails even while the other three remain.
    _SectionContract(
        document="dag",
        anchor="dependency-ordering",
        shared=("SkipRecord", "selected", "(issue_number, reason, detail)"),
        ja=("Issue番号昇順", "スコア順位", "再選定しない"),
        en=(
            "ascending Issue-number order",
            "score order",
            "does not reselect",
        ),
    ),
    _SectionContract(
        document="state",
        anchor="dependency-record-postconditions",
        shared=(
            "record_completion",
            "record_launch",
            "record_transition",
            "APPLIED",
            "NOOP",
            "CONFLICT",
        ),
    ),
    _SectionContract(
        document="state",
        anchor="dependency-fresh-validation",
        shared=("pre-execution validation", "evaluate_fresh_dependencies"),
    ),
    _SectionContract(
        document="overview",
        anchor="dependency-cycle-context",
        shared=("CycleContext",),
        ja=("公開`DispatchSnapshot`や サイクル凍結点は導入しません",),
        en=("neither a public `DispatchSnapshot` nor a cycle freeze point",),
    ),
)


@dataclass(frozen=True)
class _FallbackRow:
    """The meaning of one consumer row in the shared stack-target fallback table.

    `ordered` names code spans whose order encodes the meaning: for base
    selection, the parent branch is the configured case and `origin/main` the
    fallback, so swapping the two conditions reorders them and is rejected.
    """

    path: str
    required: tuple[str, ...]
    ordered: tuple[str, ...] = field(default=())


FALLBACK_CONTRACT = {
    "dependency-fallback-launch": _FallbackRow(
        path="launch", required=("no stack launch",)
    ),
    "dependency-fallback-rebase": _FallbackRow(
        path="rebase", required=("no stack rebase",)
    ),
    "dependency-fallback-base": _FallbackRow(
        path="base selection",
        required=("`parent/issue-{N}`", "`origin/main`"),
        ordered=("`parent/issue-{N}`", "`origin/main`"),
    ),
}

CONTRACT_LINKS = {
    "overview": (
        "architecture/dag-and-scheduling.md#dependency-three-layers",
        "architecture/state-recovery.md#dependency-record-postconditions",
        "architecture/integration.md#dependency-target-fallback",
    ),
    "dag": ("integration.md#dependency-target-fallback",),
}

_ANCHOR_TAG = re.compile(r'<a id="[a-z0-9-]+"></a>')
_HEADING = re.compile(r"^(#{1,6})\s")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_documents(language: str) -> dict[str, str]:
    return {name: _read(path) for name, path in DOCUMENTS[language].items()}


def _anchor_section(text: str, anchor: str) -> str:
    """Return only the text owned by `anchor`.

    The section runs from the anchor tag to whichever comes first: the next
    anchor tag, or the next heading at the same level as (or above) the
    section's own heading.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if f'<a id="{anchor}"></a>' in line]
    assert len(starts) == 1, (anchor, len(starts))
    start = starts[0]
    level: int | None = None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _ANCHOR_TAG.search(lines[index]):
            end = index
            break
        heading = _HEADING.match(lines[index])
        if heading is None:
            continue
        if level is None:
            level = len(heading.group(1))
        elif len(heading.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]) + "\n"


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _fallback_row_line(text: str, row_id: str) -> str:
    matching = [line for line in text.splitlines() if f"`{row_id}`" in line]
    assert len(matching) == 1, (row_id, len(matching))
    return matching[0]


def _fallback_cells(text: str, row_id: str) -> list[str]:
    row = _fallback_row_line(text, row_id).strip().strip("|")
    return [cell.strip() for cell in row.split("|")]


def _check_section_contracts(language: str, documents: dict[str, str]) -> None:
    """Assert every #875 contract appears in the anchor section that owns it."""
    for document, anchors in ANCHORS.items():
        for anchor in anchors:
            assert documents[document].count(f'<a id="{anchor}"></a>') == 1, (
                document,
                anchor,
            )

    for contract in SECTION_CONTRACTS:
        section = _normalized(
            _anchor_section(documents[contract.document], contract.anchor)
        )
        for phrase in contract.expected(language):
            assert phrase in section, (language, contract.anchor, phrase)


def _check_fallback_rows(integration: str) -> None:
    """Assert each fallback row still carries the meaning defined above."""
    for row_id, contract in FALLBACK_CONTRACT.items():
        cells = _fallback_cells(integration, row_id)
        assert len(cells) == 3, (row_id, cells)
        assert cells[0] == f"`{row_id}`", (row_id, cells[0])
        assert cells[1] == contract.path, (row_id, cells[1])
        meaning = _normalized(cells[2])
        for phrase in contract.required:
            assert phrase in meaning, (row_id, phrase, meaning)
        positions = [meaning.index(token) for token in contract.ordered]
        assert positions == sorted(positions), (row_id, contract.ordered, meaning)


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_architecture_contract_is_documented(language: str) -> None:
    _check_section_contracts(language, _load_documents(language))


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_fallback_rows_have_stable_meanings(language: str) -> None:
    _check_fallback_rows(_read(DOCUMENTS[language]["integration"]))


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_architecture_local_links_exist(language: str) -> None:
    for path in DOCUMENTS[language].values():
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(path)):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative_path = target.split("#", 1)[0]
            if relative_path:
                assert (path.parent / relative_path).resolve().exists(), (
                    path,
                    target,
                )


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_contract_link_anchors_exist(language: str) -> None:
    for document, targets in CONTRACT_LINKS.items():
        source = DOCUMENTS[language][document]
        text = _read(source)
        for target in targets:
            assert f"]({target})" in text
            relative_path, anchor = target.split("#", 1)
            destination = (source.parent / relative_path).resolve()
            assert destination.exists()
            assert f'<a id="{anchor}"></a>' in _read(destination)


def test_documented_cycle_context_record_apis_exist() -> None:
    for method in ("record_completion", "record_launch", "record_transition"):
        assert callable(getattr(CycleContext, method))


# --- Regression: the contract must detect these mutations (#911) -------------
#
# These reproduce the two mutations recorded in Issue #911 plus a section-scope
# mutation. Each one deletes or reverses a documented contract while leaving the
# surrounding document intact, so a contract that only searches the whole
# document for words keeps passing. They are the reason the checks above are
# section scoped and compare meanings rather than word presence.


def _mutate(documents: dict[str, str], name: str, old: str, new: str) -> dict[str, str]:
    mutated = dict(documents)
    assert old in mutated[name], (name, old)
    mutated[name] = mutated[name].replace(old, new)
    return mutated


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_gutted_ordering_section_is_rejected(language: str) -> None:
    """#911(1): collapsing the ordering section to its anchor must fail."""
    documents = _load_documents(language)
    section = _anchor_section(documents["dag"], "dependency-ordering")
    mutated = _mutate(
        documents,
        "dag",
        section,
        '<a id="dependency-ordering"></a>\n\nSkipRecord selected\n',
    )
    with pytest.raises(AssertionError):
        _check_section_contracts(language, mutated)


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_reversed_base_fallback_row_is_rejected(language: str) -> None:
    """#911(2): swapping the parent/main fallback conditions must fail."""
    documents = _load_documents(language)
    row = _fallback_row_line(documents["integration"], "dependency-fallback-base")
    reversed_row = (
        "| `dependency-fallback-base` | base selection | "
        "fall back to `origin/main` when a parent Issue is configured, "
        "otherwise `parent/issue-{N}` |"
    )
    mutated = _mutate(documents, "integration", row, reversed_row)
    with pytest.raises(AssertionError):
        _check_fallback_rows(mutated["integration"])


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_contract_moved_out_of_its_section_is_rejected(language: str) -> None:
    """A contract sentence parked outside its anchor section must fail."""
    documents = _load_documents(language)
    section = _anchor_section(documents["dag"], "dependency-stack-contract")
    assert "A depends on B" in section
    mutated = _mutate(
        documents,
        "dag",
        section,
        section.replace("A depends on B", "the dependent and its dependency"),
    )
    mutated["dag"] += "\n\nAppendix: A depends on B.\n"
    with pytest.raises(AssertionError):
        _check_section_contracts(language, mutated)


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_each_ordering_guarantee_is_required(language: str) -> None:
    """Deleting any single ordering guarantee must fail on its own.

    Covers candidate Issue-number order, `SkipRecord` order, the selected score
    ranking, and the ban on reselecting within one batch.
    """
    documents = _load_documents(language)
    contract = next(c for c in SECTION_CONTRACTS if c.anchor == "dependency-ordering")
    section = _anchor_section(documents["dag"], "dependency-ordering")
    for phrase in contract.expected(language):
        mutated = _mutate(documents, "dag", section, section.replace(phrase, "..."))
        with pytest.raises(AssertionError):
            _check_section_contracts(language, mutated)
