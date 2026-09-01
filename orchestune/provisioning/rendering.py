"""Render and validate Issue bodies produced by provisioning."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from orchestune.dag.models import SubTask
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    parent_issue_number_from_body,
)
from orchestune.labels import StatusLabel
from orchestune.replan.managed_body import (
    GENERATED_SUBTASK_END,
    ensure_managed_body,
    with_runtime_metadata,
)
from orchestune.symbol_verification import find_missing_symbols

_PLACEHOLDERS = (
    "subtask_id",
    "subtask_id_yaml",
    "description",
    "overview",
    "proposed_changes",
    "acceptance_criteria",
    "verification_plan",
    "footprint",
    "symbols",
    "depends_on",
    "shared_contract",
    "writes_shared_contract",
    "parent_issue_number",
    "execution_profile",
    "model_tier",
)
_PLACEHOLDER_PATTERN = re.compile(
    "{{(" + "|".join(re.escape(name) for name in _PLACEHOLDERS) + ")}}"
)


def derive_subtask_labels(
    subtask: SubTask, *, dependencies_done: bool
) -> tuple[str, ...]:
    labels = [
        StatusLabel.BLOCKED
        if subtask.depends_on and not dependencies_done
        else StatusLabel.QUEUED,
        f"priority:{subtask.priority}",
    ]
    if subtask.risk:
        labels.append("risk:flagged")
    return tuple(labels)


def subtask_issue_title(subtask: SubTask) -> str:
    return f"[FEAT] {subtask.id}: {subtask.description}"


def _yaml_inline_list(items: Sequence[str]) -> str:
    return yaml.dump(list(items), default_flow_style=True, allow_unicode=True).strip()


def _yaml_scalar(value: str) -> str:
    """Render a safe scalar without YAML's bare-document ``...`` marker.

    Dumping a throwaway mapping keeps a scalar containing ``:`` or ``#``
    correctly quoted while avoiding a document-end marker inside the enclosing
    Footprint YAML fence.
    """
    return (
        yaml.dump({"k": value}, allow_unicode=True, default_flow_style=False)
        .removeprefix("k: ")
        .rstrip("\n")
    )


def _bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "特になし"


def _render_issue_body(
    subtask: SubTask, template: str, parent_issue_number: int | None = None
) -> str:
    values = {
        "subtask_id": subtask.id,
        "subtask_id_yaml": _yaml_scalar(subtask.id),
        "description": subtask.description,
        "overview": subtask.overview or "特になし",
        "proposed_changes": _bullet_list(subtask.proposed_changes),
        "acceptance_criteria": _bullet_list(subtask.acceptance_criteria),
        "verification_plan": _bullet_list(subtask.verification_plan),
        "footprint": _yaml_inline_list(subtask.footprint),
        "symbols": _yaml_inline_list(subtask.symbols),
        "depends_on": _yaml_inline_list(subtask.depends_on),
        "shared_contract": (
            "null"
            if subtask.shared_contract is None
            else _yaml_scalar(subtask.shared_contract)
        ),
        "writes_shared_contract": (
            "true" if subtask.writes_shared_contract else "false"
        ),
        "parent_issue_number": "null"
        if parent_issue_number is None
        else str(parent_issue_number),
        "execution_profile": (
            "null"
            if subtask.execution_profile is None
            else _yaml_scalar(subtask.execution_profile)
        ),
        "model_tier": (
            "null" if subtask.model_tier is None else _yaml_scalar(subtask.model_tier)
        ),
    }
    return _PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(1)], template)


def _append_symbol_warning(body: str, subtask: SubTask, repo_root: Path) -> str:
    missing = find_missing_symbols(subtask, repo_root)
    if not missing:
        return body
    symbols = "\n".join(f"- `{symbol}`" for symbol in missing)
    warning = (
        "\n\n---\n\n⚠️ **symbols未検出**: 以下のシンボルは、Footprintに列挙されたファイル内に"
        "見つかりませんでした。このsubtaskで新規追加する予定であれば問題ありません"
        "が、リファクタによる改名・移動で古い名称が残っている可能性もあるため、"
        f"着手前にコードを確認してください。\n{symbols}"
    )
    if GENERATED_SUBTASK_END in body:
        end = body.index(GENERATED_SUBTASK_END)
        return f"{body[:end].rstrip()}{warning}\n{body[end:]}"
    return body + warning


def build_subtask_issue_body(
    subtask: SubTask,
    template: str,
    repo_root: Path,
    parent_issue_number: int | None = None,
    *,
    runtime_metadata: Mapping[str, object] | None = None,
) -> str:
    rendered = ensure_managed_body(
        _append_symbol_warning(
            _render_issue_body(subtask, template, parent_issue_number),
            subtask,
            repo_root,
        )
    )
    return with_runtime_metadata(rendered, runtime_metadata)


# Compatibility aliases for callers that used the pre-#694 private helpers.
_derive_labels = derive_subtask_labels
_build_subtask_issue_body = build_subtask_issue_body


def _subtask_id_from_body(body: str) -> str | None:
    match = FOOTPRINT_BLOCK_PATTERN.search(body or "")
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    subtask_id = data.get("subtask_id")
    return str(subtask_id) if subtask_id else None


def _depends_on_from_body(body: str) -> tuple[str, ...]:
    match = FOOTPRINT_BLOCK_PATTERN.search(body or "")
    if not match:
        return ()
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return ()
    return (
        tuple(str(item) for item in data.get("depends_on") or [])
        if isinstance(data, dict)
        else ()
    )


def _execution_profile_from_body(body: str) -> str | None:
    match = FOOTPRINT_BLOCK_PATTERN.search(body or "")
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    profile = data.get("execution_profile")
    return str(profile) if profile is not None else None


def _model_tier_from_body(body: str) -> str | None:
    match = FOOTPRINT_BLOCK_PATTERN.search(body or "")
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    tier = data.get("model_tier")
    return str(tier) if tier is not None else None


def _make_probe(
    probe_id: str,
    probe_deps: tuple[str, ...],
    profile: str | None,
    model_tier: str | None = None,
) -> SubTask:
    return SubTask(
        id=probe_id,
        description="",
        footprint=(),
        symbols=(),
        depends_on=probe_deps,
        risk=False,
        risk_reasons=(),
        execution_profile=profile,
        model_tier=model_tier,
    )


def _validate_template_profile_and_tier_markers(
    rendered: str, rendered_none: str, template_path: str | Path
) -> None:
    if _execution_profile_from_body(rendered) != "probe-profile":
        raise ValueError(
            f"{template_path} から execution_profile を再照合できません"
            "（'{{execution_profile}}' がFootprint YAMLフェンス内の"
            "'execution_profile:' として描画されていません）。実行プロファイルが永続化されません"
        )
    if _execution_profile_from_body(rendered_none) is not None:
        raise ValueError(
            f"{template_path} から execution_profile(null) を再照合できません"
            "（'{{execution_profile}}' が引用符等で囲まれているため、未指定時に'null'文字列として解釈されます）"
        )
    if _model_tier_from_body(rendered) != "strong":
        raise ValueError(
            f"{template_path} から model_tier を再照合できません"
            "（'{{model_tier}}' がFootprint YAMLフェンス内の"
            "'model_tier:' として描画されていません）。モデル能力ランクが永続化されません"
        )
    if _model_tier_from_body(rendered_none) is not None:
        raise ValueError(
            f"{template_path} から model_tier(null) を再照合できません"
            "（'{{model_tier}}' が引用符等で囲まれているため、未指定時に'null'文字列として解釈されます）"
        )


def _validate_template_identity_marker(
    template: str, template_path: str | Path
) -> None:
    probe_id = "orchestune-template-probe: needs-quoting #1"
    probe_deps = ("orchestune-template-probe-dep",)
    probe = _make_probe(probe_id, probe_deps, "probe-profile", "strong")
    rendered = _render_issue_body(probe, template)

    if _subtask_id_from_body(rendered) != probe_id:
        raise ValueError(
            f"{template_path} から subtask_id を再照合できません"
            "（'{{subtask_id_yaml}}' がFootprint YAMLフェンス内の"
            "'subtask_id:' として描画されていません）。冪等性が壊れます"
        )
    if _depends_on_from_body(rendered) != probe_deps:
        raise ValueError(
            f"{template_path} から depends_on を再照合できません"
            "（'{{depends_on}}' がFootprint YAMLフェンス内の"
            "'depends_on:' として描画されていません）。ネイティブ"
            "blocked_by関係が使えない環境で依存関係の解決が壊れます"
        )
    if (
        parent_issue_number_from_body(_render_issue_body(probe, template, 999999))
        != 999999
    ):
        raise ValueError(
            f"{template_path} から parent_issue_number を再照合できません"
            "（'{{parent_issue_number}}' がFootprint YAMLフェンス内の"
            "'parent_issue_number:' として描画されていません）。ネイティブ"
            "Sub-issue関係が使えない環境でDispatcherが子Issueを発見できなくなります"
        )
    rendered_none = _render_issue_body(
        _make_probe(probe_id, probe_deps, None, None), template
    )
    _validate_template_profile_and_tier_markers(rendered, rendered_none, template_path)


__all__ = [
    "build_subtask_issue_body",
    "derive_subtask_labels",
    "subtask_issue_title",
]
