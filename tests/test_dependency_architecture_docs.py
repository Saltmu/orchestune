"""Mechanical contracts for the bilingual dependency architecture documents."""

from __future__ import annotations

import re
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

FALLBACK_CONTRACT = {
    "dependency-fallback-launch": ("no stack", "launch"),
    "dependency-fallback-rebase": ("no stack", "rebase"),
    "dependency-fallback-base": ("parent", "main"),
}

CONTRACT_LINKS = {
    "overview": (
        "architecture/dag-and-scheduling.md#dependency-three-layers",
        "architecture/state-recovery.md#dependency-record-postconditions",
        "architecture/integration.md#dependency-target-fallback",
    ),
    "dag": ("integration.md#dependency-target-fallback",),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_architecture_contract_is_documented(language: str) -> None:
    documents = {name: _read(path) for name, path in DOCUMENTS[language].items()}

    for document, anchors in ANCHORS.items():
        for anchor in anchors:
            assert documents[document].count(f'<a id="{anchor}"></a>') == 1

    dag = documents["dag"]
    for token in (
        "Identity Resolution",
        "Lifecycle Assessment",
        "Use-case Policy",
        "COMPLETED > CHANGES_REQUESTED > CI_PASSED_UNMERGED > WAITING",
        "NOT_NEEDED",
        "subtask_id",
        "A depends on B",
        "SkipRecord",
        "selected",
    ):
        assert token in dag

    state = documents["state"]
    for token in (
        "record_completion",
        "record_launch",
        "record_transition",
        "APPLIED",
        "NOOP",
        "CONFLICT",
        "pre-execution validation",
        "evaluate_fresh_dependencies",
    ):
        assert token in state

    overview = documents["overview"]
    assert "CycleContext" in overview
    assert "DispatchSnapshot" in overview
    assert "dependency-three-layers" in overview
    assert "dependency-record-postconditions" in overview
    assert "dependency-target-fallback" in overview
    normalized_overview = " ".join(overview.split())
    if language == "ja":
        assert "公開`DispatchSnapshot`や サイクル凍結点は導入しません" in (
            normalized_overview
        )
    else:
        assert "neither a public `DispatchSnapshot` nor a cycle freeze point" in (
            normalized_overview
        )


@pytest.mark.parametrize("language", sorted(DOCUMENTS))
def test_dependency_fallback_rows_have_stable_meanings(language: str) -> None:
    text = _read(DOCUMENTS[language]["integration"])

    for row_id, meaning_tokens in FALLBACK_CONTRACT.items():
        matching_lines = [line for line in text.splitlines() if f"`{row_id}`" in line]
        assert len(matching_lines) == 1
        normalized = matching_lines[0].lower()
        assert all(token in normalized for token in meaning_tokens)


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
