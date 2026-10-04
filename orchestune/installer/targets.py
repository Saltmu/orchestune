from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from orchestune.infra.session_dirs import find_project_root
from orchestune.installer.contracts import (
    PhysicalRoot,
    ScopeType,
    TargetResolutionError,
    TargetType,
)


def _normalize_targets(targets: list[TargetType]) -> list[TargetType]:
    if TargetType.ALL in targets:
        if len(targets) > 1:
            raise TargetResolutionError("Cannot mix 'all' with specific targets")
        return [
            TargetType.CODEX,
            TargetType.CLAUDE,
            TargetType.ANTIGRAVITY,
            TargetType.ANTIGRAVITY_CLI,
        ]
    return list(dict.fromkeys(targets))


def _resolve_target_path(
    target: TargetType,
    scope: ScopeType,
    effective_project: Path,
    effective_home: Path,
) -> Path:
    if scope == ScopeType.PROJECT:
        if target in (
            TargetType.CODEX,
            TargetType.ANTIGRAVITY,
            TargetType.ANTIGRAVITY_CLI,
        ):
            return effective_project / ".agents" / "skills"
        if target == TargetType.CLAUDE:
            return effective_project / ".claude" / "skills"
        raise TargetResolutionError(f"Unsupported target for project scope: {target}")

    if scope == ScopeType.USER:
        if target == TargetType.CODEX:
            return effective_home / ".agents" / "skills"
        if target == TargetType.CLAUDE:
            return effective_home / ".claude" / "skills"
        if target == TargetType.ANTIGRAVITY:
            return effective_home / ".gemini" / "config" / "skills"
        if target == TargetType.ANTIGRAVITY_CLI:
            return effective_home / ".gemini" / "antigravity-cli" / "skills"
        raise TargetResolutionError(f"Unsupported target for user scope: {target}")

    raise TargetResolutionError(f"Unsupported scope: {scope}")


def resolve_targets(
    targets: list[TargetType],
    scope: ScopeType,
    project_dir: Path | None = None,
    home: Path | None = None,
    skills_dir: Path | None = None,
) -> list[PhysicalRoot]:
    resolved_target_types = _normalize_targets(targets)

    if skills_dir is not None:
        if len(resolved_target_types) != 1 or TargetType.ALL in targets:
            raise TargetResolutionError(
                "--skills-dir can only be used with a single target"
            )
        return [
            PhysicalRoot(
                path=skills_dir.resolve(),
                target_types=resolved_target_types,
                scope=scope,
                is_custom_skills_dir=True,
            )
        ]

    effective_home = (home or Path.home()).resolve()
    effective_project = (project_dir or find_project_root()).resolve()

    # Map each target to its relative path under project or user
    roots_by_path: dict[Path, list[TargetType]] = defaultdict(list)

    for target in resolved_target_types:
        path = _resolve_target_path(target, scope, effective_project, effective_home)
        roots_by_path[path].append(target)

    # Sort deterministically
    sorted_paths = sorted(roots_by_path.keys())
    return [
        PhysicalRoot(
            path=p,
            target_types=sorted(roots_by_path[p], key=lambda t: t.value),
            scope=scope,
            is_custom_skills_dir=False,
        )
        for p in sorted_paths
    ]
