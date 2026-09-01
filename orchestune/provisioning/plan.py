"""Plan metadata and parent-plan persistence for Issue provisioning."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestune.dag.parsing import extract_frontmatter_and_body
from orchestune.forge import IssueForge
from orchestune.issue_parsing import (
    PARENT_MARKER,
    embed_decomposition_plan_in_parent_body,
    restore_plan_markdown_from_parent_body,
)
from orchestune.provisioning.plan_loading import PlanMetadata as PlanMetadata
from orchestune.provisioning.plan_loading import load_plan

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
) -> Path:
    parent_issue = forge.get_issue(parent_issue_number)
    if parent_issue is None:
        raise ValueError(
            f"Parent issue #{parent_issue_number} was not found on the forge."
        )
    restored_markdown = restore_plan_markdown_from_parent_body(parent_issue.body)
    if not restored_markdown:
        raise ValueError(
            f"Parent issue #{parent_issue_number} does not contain a valid decomposition plan block."
        )
    out_file = Path(output_path)
    out_file.write_text(restored_markdown, encoding="utf-8")
    return out_file


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
        updated_body = embed_decomposition_plan_in_parent_body(
            parent_issue.body, raw_frontmatter
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
