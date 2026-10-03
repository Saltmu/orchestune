"""Parse and structurally validate LLM per-finding judgments, never judge prose."""

from __future__ import annotations

import re
from typing import Any

import yaml

JUDGMENTS = frozenset(
    {"adopt", "decline", "already_addressed", "needs_information", "duplicate"}
)
STATUSES = frozenset({"unresolved", "resolved", "declined", "deferred"})
FIELDS = ("source", "location", "judgment", "status", "basis", "evidence")


def parse_judgments(body: str) -> dict[str, Any]:
    """Require exactly one fenced YAML table; round identifies the judged round."""
    blocks = re.findall(
        r"^```orchestune-review-judgments[^\S\n]*\n(.*?)^```[^\S\n]*$",
        body,
        re.M | re.S,
    )
    if len(blocks) != 1:
        raise ValueError("exactly one orchestune-review-judgments block is required")
    try:
        data = yaml.safe_load(blocks[0])
    except yaml.YAMLError as exc:
        raise ValueError("invalid review judgments YAML") from exc
    return validate_judgments(data)


def validate_judgments(data: object) -> dict[str, Any]:
    """Validate required fields and enums, including a basis for deferred items."""
    if not isinstance(data, dict):
        raise ValueError("judgments must be a mapping")
    round_num = data.get("round")
    if not isinstance(round_num, int) or isinstance(round_num, bool) or round_num < 1:
        raise ValueError("judgment round must be a positive integer")
    rows = data.get("findings")
    if not isinstance(rows, list):
        raise ValueError("findings must be a list (empty for zero findings)")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each finding must be a mapping")
        for field in FIELDS:
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"finding {field} must be a nonempty string")
        if row["judgment"] not in JUDGMENTS or row["status"] not in STATUSES:
            raise ValueError("invalid finding judgment/status")
    return data


def finding_source(item: dict[str, Any], kind: str) -> str:
    """Stable container identity; use separate rows for multiple findings in it."""
    identity = item.get("html_url") or item.get("url")
    if item.get("id") is not None:
        return f"{kind}:{item['id']}"
    if isinstance(identity, str) and identity:
        return identity
    raise ValueError("current review item lacks a source id/URL")


def current_sources(result: dict[str, Any]) -> set[str]:
    """Conservatively require every nonempty current body, even a clean summary.

    Finding extraction remains an LLM responsibility. A clean summary can be
    represented as already_addressed with a basis stating that no findings exist.
    """
    sources: set[str] = set()
    for section, default_kind in (
        ("review_items", "issue_comment"),
        ("inline_comments", "inline_comment"),
    ):
        for item in result.get(section, []):
            if (
                item.get("provenance") == "current"
                and str(item.get("body") or "").strip()
            ):
                sources.add(finding_source(item, item.get("kind") or default_kind))
    return sources


def validate_coverage(judgments: dict[str, Any], result: dict[str, Any]) -> None:
    """Check previous-round identity and coverage, leaving semantic verdicts to LLM."""
    validate_judgments(judgments)
    if judgments["round"] != result.get("round"):
        raise ValueError("judgment round must match the previous review round")
    covered = {row["source"] for row in judgments["findings"]}
    missing = current_sources(result) - covered
    if missing:
        raise ValueError(
            f"judgments omit current sources: {', '.join(sorted(missing))}"
        )
