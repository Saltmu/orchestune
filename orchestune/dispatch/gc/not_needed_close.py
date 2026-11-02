"""Helper for closing and confirming not-needed issues without bloating completion.py."""

from __future__ import annotations

from typing import Any

from orchestune.labels import StatusLabel


def confirm_not_needed_close(forge: Any, issue_number: int) -> None:
    forge.close_issue(
        issue_number,
        "not planned",
        comment="対応不要（status:not-needed）と判定されたため、Orchestuneが自動的にクローズしました。",
    )
    issue = forge.get_issue(issue_number)
    if (
        issue is None
        or issue.state.upper() != "CLOSED"
        or StatusLabel.NOT_NEEDED.value not in issue.labels
    ):
        raise RuntimeError(
            f"Could not confirm not-needed close for #{issue_number}; retrying"
        )
