"""Static detection of project development workflow skills and discipline docs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestune.installer.payload import STANDARD_SKILLS, WORKFLOW_SKILL
from orchestune.installer.state import INSTALLER_DIR

WorkflowStatus = Literal[
    "missing",
    "workflow_template",
    "custom_complete",
    "merge_recommended",
]

DISCIPLINE_CANDIDATES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/AGENTS.md",
)
PROJECT_SKILL_ROOT_RELS: tuple[tuple[str, ...], ...] = (
    (".agents", "skills"),
    (".claude", "skills"),
    (".grok", "skills"),
)
TEMPLATE_PLACEHOLDERS: tuple[str, ...] = (
    "<CI_ENTRYPOINT>",
    "<TEST_COMMAND>",
    "<PREFLIGHT_CHECK_COMMAND>",
    "<INSTALL_COMMAND>",
)

_CLAIM_RE = re.compile(r"\borchestune\s+claim\b")
_COMPLETE_RE = re.compile(r"\borchestune\s+complete\b")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_MAX_LINK_DEPTH = 3
_MAX_DISCIPLINE_FILES = 64

MERGE_CHECKLIST: tuple[str, ...] = (
    "タスク開始前に `orchestune claim <issue_number> --apply` で隔離 worktree を準備し、完了時も worktree を削除しない",
    "GitHub Issue / PR ラベルを `gh issue edit --add-label` 等で直接変更しない（Orchestune 本体に委譲）",
    "PR 作成時は指定ベースブランチを使用し、レビュー結果を `<session-dir>/review-reply.md` に記録する",
    "完了時に必ず `orchestune complete --apply --issue <N> --pr <PR> --result done --reviewer <claude|codex|skip> --review-reply <session-dir>/review-reply.md`（または `blocked` / `not-needed`）を実行する",
)


@dataclass(frozen=True)
class ProjectWorkflowDetection:
    status: WorkflowStatus
    discipline_files: tuple[str, ...]
    custom_skills: tuple[str, ...]
    workflow_templates: tuple[str, ...]
    has_claim: bool
    has_complete: bool
    missing_procedures: tuple[str, ...]
    template_has_placeholders: bool
    merge_checklist: tuple[str, ...]


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _is_within_root(path: Path, resolved_root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved.is_relative_to(resolved_root)


def _format_rel_path(path: Path, resolved_root: Path | None) -> str:
    if resolved_root is not None:
        try:
            return path.resolve().relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            pass
    return path.as_posix()


def _extract_relative_md_targets(content: str, current_file: Path) -> list[Path]:
    targets: list[Path] = []
    for raw_target in _MD_LINK_RE.findall(content):
        cleaned = raw_target.split("#", 1)[0].split("?", 1)[0].strip()
        if not cleaned or "://" in cleaned or cleaned.startswith(("mailto:", "/")):
            continue
        if not cleaned.lower().endswith(".md"):
            continue
        targets.append(current_file.parent / cleaned)
    return targets


def _scan_discipline_docs(
    resolved_root: Path | None,
) -> tuple[tuple[str, ...], list[str]]:
    if resolved_root is None:
        return (), []

    found_entries: list[str] = []
    texts: list[str] = []
    visited: set[Path] = set()
    queue: list[tuple[Path, int]] = []

    for rel in DISCIPLINE_CANDIDATES:
        candidate = resolved_root / rel
        if candidate.is_file() and _is_within_root(candidate, resolved_root):
            found_entries.append(rel)
            queue.append((candidate.resolve(), 0))

    while queue and len(visited) < _MAX_DISCIPLINE_FILES:
        current, depth = queue.pop(0)
        if current in visited or not current.is_file():
            continue
        visited.add(current)
        content = _read_text_safe(current)
        if content is None:
            continue
        texts.append(content)
        if depth >= _MAX_LINK_DEPTH:
            continue
        for target in _extract_relative_md_targets(content, current):
            if _is_within_root(target, resolved_root):
                resolved_target = target.resolve()
                if resolved_target not in visited and resolved_target.is_file():
                    queue.append((resolved_target, depth + 1))

    return tuple(found_entries), texts


def _collect_skill_roots(
    resolved_root: Path | None, extra_skills_roots: Sequence[Path]
) -> list[Path]:
    candidates: list[Path] = []
    if resolved_root is not None:
        for parts in PROJECT_SKILL_ROOT_RELS:
            candidates.append(resolved_root.joinpath(*parts))
    candidates.extend(extra_skills_roots)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in candidates:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            deduped.append(resolved)
    deduped.sort(key=lambda p: p.as_posix())
    return deduped


def _read_skill_texts(skill_dir: Path, resolved_root: Path | None) -> list[str]:
    texts: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return texts
    if resolved_root is not None and not _is_within_root(skill_md, resolved_root):
        return texts
    content = _read_text_safe(skill_md)
    if content is not None:
        texts.append(content)

    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            if not ref_file.is_file():
                continue
            if resolved_root is not None and not _is_within_root(
                ref_file, resolved_root
            ):
                continue
            ref_text = _read_text_safe(ref_file)
            if ref_text is not None:
                texts.append(ref_text)
    return texts


def _scan_project_skills(
    resolved_root: Path | None,
    extra_skills_roots: Sequence[Path],
) -> tuple[tuple[str, ...], tuple[str, ...], list[str], bool]:
    excluded = set(STANDARD_SKILLS) | {INSTALLER_DIR}
    custom_skills: list[str] = []
    workflow_templates: list[str] = []
    custom_texts: list[str] = []
    template_texts: list[str] = []

    for skills_root in _collect_skill_roots(resolved_root, extra_skills_roots):
        try:
            entries = sorted(skills_root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name in excluded:
                continue
            if resolved_root is not None and not _is_within_root(entry, resolved_root):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            rel_skill_md = _format_rel_path(skill_md, resolved_root)
            texts = _read_skill_texts(entry, resolved_root)
            if entry.name == WORKFLOW_SKILL:
                workflow_templates.append(rel_skill_md)
                template_texts.extend(texts)
            else:
                custom_skills.append(rel_skill_md)
                custom_texts.extend(texts)

    template_has_placeholders = any(
        placeholder in text
        for text in template_texts
        for placeholder in TEMPLATE_PLACEHOLDERS
    )
    return (
        tuple(dict.fromkeys(custom_skills)),
        tuple(dict.fromkeys(workflow_templates)),
        custom_texts + template_texts,
        template_has_placeholders,
    )


def detect_project_workflow(
    project_root: Path | None,
    *,
    extra_skills_roots: Sequence[Path] = (),
) -> ProjectWorkflowDetection:
    """Detect existing discipline docs, custom skills, and workflow-template coverage."""
    resolved_root = project_root.resolve() if project_root is not None else None
    discipline_files, discipline_texts = _scan_discipline_docs(resolved_root)
    custom_skills, workflow_templates, skill_texts, template_has_placeholders = (
        _scan_project_skills(resolved_root, extra_skills_roots)
    )

    combined = "\n".join(discipline_texts + skill_texts)
    has_claim = bool(_CLAIM_RE.search(combined))
    has_complete = bool(_COMPLETE_RE.search(combined))
    missing: list[str] = []
    if not has_claim:
        missing.append("orchestune claim")
    if not has_complete:
        missing.append("orchestune complete")

    if discipline_files or custom_skills:
        status: WorkflowStatus = (
            "custom_complete" if has_complete else "merge_recommended"
        )
    elif workflow_templates:
        status = "workflow_template"
    else:
        status = "missing"

    checklist = MERGE_CHECKLIST if status == "merge_recommended" else ()
    return ProjectWorkflowDetection(
        status=status,
        discipline_files=discipline_files,
        custom_skills=custom_skills,
        workflow_templates=workflow_templates,
        has_claim=has_claim,
        has_complete=has_complete,
        missing_procedures=tuple(missing),
        template_has_placeholders=template_has_placeholders,
        merge_checklist=checklist,
    )


def format_merge_guidance_lines(detection: ProjectWorkflowDetection) -> tuple[str, ...]:
    """Build human-readable merge proposal lines for existing docs/skills."""
    targets = list(detection.discipline_files) + list(detection.custom_skills)
    lines: list[str] = []
    if targets:
        lines.append(f"検出された既存開発スキル／規律文書: {', '.join(targets)}")
    if detection.missing_procedures:
        lines.append(
            f"不足している必須手順: {', '.join(detection.missing_procedures)}（claim のみ動作し complete が呼ばれない恐れがあります）"
        )
    for item in detection.merge_checklist:
        lines.append(f"  - {item}")
    lines.append(
        "対応方法: 既存ファイルへ上記手順をマージするか、AI アシスタントで `orchestune-readiness` スキルを実行してマージ差分案を作成してください。"
    )
    lines.append(
        "参照用テンプレートが必要な場合は `orchestune skills install --target all --scope project --with-workflow-skill` を実行できます。"
    )
    return tuple(lines)
