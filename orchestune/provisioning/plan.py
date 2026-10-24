"""Plan metadata and parent-plan persistence for Issue provisioning."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestune.dag.parsing import extract_frontmatter_and_body
from orchestune.forge import IssueForge
from orchestune.issue_parsing import (
    PARENT_MARKER,
    decomposition_plan_from_parent_body,
    embed_decomposition_plan_in_parent_body,
)
from orchestune.provisioning.plan_loading import PlanMetadata as PlanMetadata
from orchestune.provisioning.plan_loading import load_plan
from orchestune.provisioning.plan_manifest import project_plan_manifest
from orchestune.provisioning.plan_restoration import (
    restore_plan_file_from_parent as _restore_plan_file_from_parent,
)

_load_plan = load_plan

# #664: GitHubのIssue本文の上限。これを超える本文はAPIに拒否されるため、
# 送る前に自分で止めて理由を明示する（拒否をそのまま握り潰すと「provisionは
# 成功したのに親Issueだけ古い」状態に気付けない）。
GITHUB_ISSUE_BODY_LIMIT = 65536


def _parent_body(
    title: str, description: str = "", plan_data: dict | str | None = None
) -> str:
    body = title
    if description:
        body += f"\n\n{description}"
    body += f"\n\n配下のサブタスクはこのIssueのSub-issueとして紐付けられます。\n\n{PARENT_MARKER}"
    return (
        embed_decomposition_plan_in_parent_body(body, plan_data)
        if plan_data is not None
        else body
    )


def restore_plan_file_from_parent(
    forge: IssueForge,
    parent_issue_number: int,
    output_path: str | Path = "decomposition_plan.md",
    *,
    template_path: str | Path | None = None,
) -> Path:
    return _restore_plan_file_from_parent(
        forge,
        parent_issue_number,
        output_path,
        template_path=template_path,
    )


def _subtask_entries(plan_data: object) -> list[dict]:
    subtasks = plan_data.get("subtasks") if isinstance(plan_data, dict) else None
    if not isinstance(subtasks, list):
        return []
    return [entry for entry in subtasks if isinstance(entry, dict)]


def parent_only_subtask_ids(
    parent_body: str, local_plan_data: object
) -> tuple[str, ...]:
    """#814: 親計画にあり、ローカル計画に同じ`id`がない登録済みsubtaskのid。

    補修タスクの登録（`orchestune remediate`）は親計画へ直接エントリを追記
    するため、それを知らない古いローカル計画で同期すると、登録済みの補修
    エントリが親計画から消える。`issue_number`を持たないエントリはprovision
    途中の状態でもありうるため、ここでは数えない。
    """
    local_ids = {str(entry.get("id")) for entry in _subtask_entries(local_plan_data)}
    return tuple(
        sorted(
            str(entry["id"])
            for entry in _subtask_entries(
                decomposition_plan_from_parent_body(parent_body)
            )
            if entry.get("id") is not None
            and entry.get("issue_number") is not None
            and str(entry["id"]) not in local_ids
        )
    )


def _warn_parent_only_subtasks(
    parent_issue_number: int, subtask_ids: tuple[str, ...]
) -> None:
    print(
        f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: "
        f"the parent plan has registered subtasks missing from the local plan "
        f"({', '.join(subtask_ids)}); restore the plan with "
        f"`orchestune provision --restore-plan {parent_issue_number}` and retry",
        file=sys.stderr,
    )


def sync_parent_decomposition_plan(
    forge: IssueForge, parent_issue_number: int, plan_path: str | Path
) -> bool:
    """Embed the current plan frontmatter in its parent Issue body."""
    try:
        parent_issue = forge.get_issue(parent_issue_number)
        if parent_issue is None:
            print(
                f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: parent issue not found",
                file=sys.stderr,
            )
            return False
        raw_frontmatter, _ = extract_frontmatter_and_body(
            Path(plan_path).read_text(encoding="utf-8")
        )
        if not raw_frontmatter:
            print(
                f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: no frontmatter in {plan_path}",
                file=sys.stderr,
            )
            return False
        if missing := parent_only_subtask_ids(parent_issue.body, raw_frontmatter):
            _warn_parent_only_subtasks(parent_issue_number, missing)
            return False
        manifest_data = project_plan_manifest(raw_frontmatter)
        updated_body = embed_decomposition_plan_in_parent_body(
            parent_issue.body, manifest_data
        )
        if len(updated_body) > GITHUB_ISSUE_BODY_LIMIT:
            print(
                f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: "
                f"the resulting body is {len(updated_body)} characters, over GitHub's "
                f"{GITHUB_ISSUE_BODY_LIMIT} character limit",
                file=sys.stderr,
            )
            return False
        if updated_body != parent_issue.body:
            forge.update_issue_body(parent_issue_number, updated_body)
        return True
    except Exception as error:
        print(
            f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: {error}",
            file=sys.stderr,
        )
        return False
