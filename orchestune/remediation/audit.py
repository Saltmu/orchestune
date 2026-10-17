"""Idempotent parent audit for each remediation spec revision."""

from typing import Any


def audit_marker(revision: str) -> str:
    return f"<!-- orchestune:remediation-audit spec_revision={revision} -->"


def audit_once(
    forge: Any, parent: int, revision: str, entries: list[dict[str, Any]]
) -> None:
    marker = audit_marker(revision)
    if any(
        marker in str(c.get("body", "")) for c in forge.list_all_issue_comments(parent)
    ):
        return
    lines = [marker, "## Remediation registration", f"Spec revision: `{revision}`"]
    for entry in entries:
        record = entry["remediation"]
        lines.extend(
            (
                f"- {entry['id']}: #{entry['issue_number']}",
                f"  - Parent acceptance: {record['parent_acceptance']}",
                f"  - Origins: {record['origins']}",
                f"  - Reason: {record['reason']}",
            )
        )
    forge.add_comment(parent, "\n".join(lines))
