"""Helpers shared by recovery bookkeeping and restoration tests."""

from __future__ import annotations

from orchestune.models import IssueRecord


def _issue_with_footprint(
    number,
    subtask_id=None,
    footprint=None,
    depends_on=None,
    blocked_by=(),
    parent=None,
    created_at="2026-01-01T00:00:00+00:00",
    recompute_count=None,
    forced_serial=None,
):
    if subtask_id is None:
        body = "本文のみでFootprintブロックなし"
    else:
        footprint_lines = (
            "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
        )
        depends_on_values = ", ".join(depends_on or ())
        extra_fields = ""
        if recompute_count is not None:
            extra_fields += f"recompute_count: {recompute_count}\n"
        if forced_serial is not None:
            extra_fields += f"forced_serial: {str(forced_serial).lower()}\n"
        body = (
            "## Footprint\n```yaml\n"
            f"subtask_id: {subtask_id}\n"
            "footprint:\n"
            f"{footprint_lines}\n"
            f"depends_on: [{depends_on_values}]\n"
            f"{extra_fields}"
            "```\n"
        )
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=("status:in-progress",),
        created_at=created_at,
        blocked_by=blocked_by,
        parent=parent,
    )
