"""Bounded, revision-aware data for Jev; never reads the working tree or fetches."""

from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, TypedDict, TypeGuard

SCHEMA_VERSION = 2
MAX_SOURCE_BYTES = 128 * 1024
MAX_PAYLOAD_BYTES = 32 * 1024
TEXT_LIMITS = {
    "pr.title": 300,
    "pr.body": 8000,
    "code.text": 6000,
    "code.module_description": 2000,
    "repository_rules.text": 8000,
}


class JevFindingContext(TypedDict):
    schema_version: int
    pr: dict[str, Any]
    code: dict[str, Any]
    execution: dict[str, Any]
    repository_rules: dict[str, Any]
    missing: list[str]
    truncated: list[str]


@dataclass
class JevReviewContext:
    """One processing unit's PR snapshot and immutable blob cache."""

    pr: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    blobs: dict[tuple[str, str], str | None] = field(default_factory=dict, repr=False)
    left_sha: str | None = field(default=None, repr=False)


def collect_review_context(pr_number: int) -> JevReviewContext:
    """Fetch metadata once; failure is context loss, not an evaluation failure."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "title,body,headRefOid,baseRefOid",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        value = json.loads(result.stdout)
        return JevReviewContext(
            pr={
                "number": pr_number,
                "title": value["title"],
                "body": value["body"],
                "head_sha": value["headRefOid"],
                "base_sha": value["baseRefOid"],
            }
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return JevReviewContext(pr={"number": pr_number}, missing=["pr"])


def _valid_sha(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None
    )


def _valid_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    return not (
        PurePosixPath(value).is_absolute()
        or any(part in ("..", ".", "") for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    )


def _read_blob(sha: str, path: str) -> str | None:
    if not _valid_sha(sha) or not _valid_path(path):
        return None
    spec = f"{sha}:{path}"
    try:
        kind = subprocess.run(
            ["git", "cat-file", "-t", spec],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if kind.stdout.strip() != "blob":
            return None
        size = subprocess.run(
            ["git", "cat-file", "-s", spec],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if not 0 <= int(size.stdout.strip()) <= MAX_SOURCE_BYTES:
            return None
        result = subprocess.run(
            ["git", "show", spec], capture_output=True, check=True, timeout=10
        )
        if len(result.stdout) > MAX_SOURCE_BYTES or b"\0" in result.stdout:
            return None
        return result.stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return None


def _blob(review: JevReviewContext, sha: str, path: str) -> str | None:
    key = (sha, path)
    if key not in review.blobs:
        review.blobs[key] = _read_blob(sha, path)
    return review.blobs[key]


def _left_revision(review: JevReviewContext) -> str:
    """PR diff LEFT refers to the merge base, not an advancing base branch tip."""
    if review.left_sha is not None:
        return review.left_sha
    review.left_sha = ""
    base, head = review.pr.get("base_sha"), review.pr.get("head_sha")
    if not _valid_sha(base) or not _valid_sha(head):
        return ""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--all", base, head],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        sha = result.stdout.strip()
        if _valid_sha(sha):
            review.left_sha = sha
    except (OSError, subprocess.SubprocessError):
        pass
    return review.left_sha


def _position(item: dict[str, Any], review: JevReviewContext) -> tuple[str, Any]:
    side = item.get("side")
    line = item.get("position_line", item.get("line"))
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        if item.get("commit_id") == review.pr.get("head_sha"):
            sha = (
                review.pr.get("head_sha", "")
                if side == "RIGHT"
                else _left_revision(review)
            )
            if side in ("RIGHT", "LEFT") and _valid_sha(sha):
                return sha, line
    elif side == "RIGHT" and _valid_sha(item.get("original_commit_id")):
        original = item.get("original_line")
        if (
            isinstance(original, int)
            and not isinstance(original, bool)
            and original > 0
        ):
            return item["original_commit_id"], original
    return "", None


def _module_description(text: str) -> str:
    try:
        tree = ast.parse(text)
        docstring = ast.get_docstring(tree, clean=False) or ""
    except (SyntaxError, ValueError):
        docstring = ""
    comments: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            comments.append(line)
        else:
            break
    return "\n".join(comments + [docstring]).strip()


def collect_finding_context(
    item: dict[str, Any], review: JevReviewContext
) -> JevFindingContext:
    """Match a comment to its revision, otherwise keep the original review diff."""
    sha, line = _position(item, review)
    path = item.get("path", "")
    path = path if isinstance(path, str) else ""
    text = _blob(review, sha, path) if sha else None
    code: dict[str, Any] = {
        "source": "review_diff",
        "commit_sha": item.get("original_commit_id")
        if item.get("position_line", item.get("line")) in (None, "N/A")
        else item.get("commit_id"),
        "side": item.get("side"),
        "start_line": item.get("start_line"),
        "text": item.get("diff_hunk") or "",
        "status": "missing",
    }
    evidence: list[dict[str, str]] = []
    if text is not None and line <= len(text.splitlines()):
        lines = text.splitlines()
        description = _module_description(text)
        code.update(
            source="git_blob",
            commit_sha=sha,
            start_line=max(1, line - 10),
            text="\n".join(lines[max(0, line - 11) : line + 10]),
            module_description=description,
        )
        if description:
            evidence.append(
                {"source": "module_description", "commit_sha": sha, "text": description}
            )
    code["status"] = "available" if code["text"] else "missing"
    base = review.pr.get("base_sha", "")
    rules_text = _blob(review, base, ".agents/AGENTS.md") if _valid_sha(base) else None
    rules = {
        "source": ".agents/AGENTS.md",
        "commit_sha": base,
        "text": rules_text or "",
        "status": "available" if rules_text else "missing",
    }
    missing = list(review.missing)
    if code["status"] == "missing":
        missing.append("code")
    if rules["status"] == "missing":
        missing.append("repository_rules")
    return normalize_context(
        {
            "pr": review.pr,
            "code": code,
            "execution": {
                "component_hint": "internal_tool_candidate"
                if str(path).startswith("scripts/")
                else "unknown",
                "input_trust": "unknown",
                "evidence": evidence,
            },
            "repository_rules": rules,
            "missing": missing,
            "truncated": [],
        }
    )


def _markers(value: Any) -> list[str]:
    return (
        [str(item)[:100] for item in value[:30] if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def normalize_context(value: Any) -> JevFindingContext:
    """Whitelist and bound offline/online context identically; invalid data is unknown."""
    raw = value if isinstance(value, dict) else {}
    context: JevFindingContext = {
        "schema_version": SCHEMA_VERSION,
        "pr": {},
        "code": {},
        "execution": {},
        "repository_rules": {},
        "missing": _markers(raw.get("missing")),
        "truncated": _markers(raw.get("truncated")),
    }
    allowed = {
        "pr": ("number", "title", "body", "head_sha", "base_sha"),
        "code": (
            "source",
            "commit_sha",
            "side",
            "start_line",
            "text",
            "module_description",
            "status",
        ),
        "execution": ("component_hint", "input_trust"),
        "repository_rules": ("source", "commit_sha", "text", "status"),
    }
    sections = {
        "pr": context["pr"],
        "code": context["code"],
        "execution": context["execution"],
        "repository_rules": context["repository_rules"],
    }
    for section, keys in allowed.items():
        data = raw.get(section)
        if not isinstance(data, dict):
            context["missing"].append(section)
            continue
        for key in keys:
            item = data.get(key)
            if isinstance(item, str):
                marker = f"{section}.{key}"
                limit = TEXT_LIMITS.get(marker, 300)
                sections[section][key] = item[:limit]
                if len(item) > limit:
                    context["truncated"].append(marker)
            elif isinstance(item, int) and not isinstance(item, bool):
                sections[section][key] = item
    for key in ("missing", "truncated"):
        if key in raw and (
            not isinstance(raw[key], list)
            or any(not isinstance(x, str) for x in raw[key])
        ):
            context["missing"].append(key)
    if "schema_version" in raw and raw["schema_version"] != SCHEMA_VERSION:
        context["missing"].append("schema_version")
    _normalize_evidence(raw, context)
    _validate_provenance(context)
    for section in ("code", "repository_rules"):
        if sections[section].get("status") != "available" or not sections[section].get(
            "text"
        ):
            context["missing"].append(section)
    context["missing"] = sorted(set(context["missing"]))
    context["truncated"] = sorted(set(context["truncated"]))
    return context


def _normalize_evidence(raw: dict[str, Any], context: JevFindingContext) -> None:
    execution = raw.get("execution", {})
    values = execution.get("evidence", []) if isinstance(execution, dict) else []
    evidence = []
    if not isinstance(values, list):
        context["missing"].append("execution.evidence")
        values = []
    if len(values) > 3:
        context["truncated"].append("execution.evidence")
    for value in values[:3]:
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            context["missing"].append("execution.evidence")
            continue
        evidence.append(
            {
                "source": str(value.get("source", "unknown"))[:100],
                "commit_sha": str(value.get("commit_sha", ""))[:40],
                "text": value["text"][:2000],
            }
        )
        if len(value["text"]) > 2000:
            context["truncated"].append("execution.evidence")
    context["execution"]["evidence"] = evidence


def _validate_provenance(context: JevFindingContext) -> None:
    code, rules, pr = context["code"], context["repository_rules"], context["pr"]
    if (
        code.get("source") not in ("git_blob", "review_diff")
        or not _valid_sha(code.get("commit_sha"))
        or code.get("side") not in ("LEFT", "RIGHT")
    ):
        context["missing"].append("code.provenance")
    if code.get("source") == "git_blob" and (
        not isinstance(code.get("start_line"), int) or code["start_line"] < 1
    ):
        context["missing"].append("code.position")
    if (
        rules.get("source") != ".agents/AGENTS.md"
        or not _valid_sha(rules.get("commit_sha"))
        or (pr.get("base_sha") and pr["base_sha"] != rules.get("commit_sha"))
    ):
        context["missing"].append("repository_rules.provenance")
    for item in context["execution"].get("evidence", []):
        expected = (
            rules.get("commit_sha")
            if item["source"] == "repository_documentation"
            else code.get("commit_sha")
        )
        if (
            item["source"] in ("module_description", "code", "repository_documentation")
            and item["commit_sha"] != expected
        ):
            context["missing"].append("execution.provenance")


def has_speculative_evidence(context: Any) -> bool:
    value = normalize_context(context)
    evidence = value["execution"].get("evidence", [])
    return (
        not value["missing"]
        and not value["truncated"]
        and any(
            item["source"] in ("module_description", "code", "repository_documentation")
            and item["text"].strip()
            for item in evidence
        )
    )


def encode_bounded_payload(payload: dict[str, Any]) -> bytes:
    """Trim whole characters, retaining explicit omission markers within 32 KiB."""
    payload = copy.deepcopy(payload)
    context = payload["state"]["context"]
    targets = [
        (context["code"], "module_description", "code.module_description"),
        (context["execution"], "evidence", "execution.evidence"),
        (context["pr"], "body", "pr.body"),
        (context["repository_rules"], "text", "repository_rules.text"),
        (context["code"], "text", "code.text"),
        (payload["state"], "comment", "comment"),
        (payload["state"], "path", "path"),
    ]
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for container, key, marker in targets:
        while len(encoded) > MAX_PAYLOAD_BYTES and container.get(key):
            item = container[key]
            container[key] = item[: len(item) // 2] if isinstance(item, str) else []
            if marker not in context["truncated"]:
                context["truncated"].append(marker)
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return encoded


def context_summary(context: Any) -> dict[str, Any]:
    value = normalize_context(context)
    return {
        "schema_version": SCHEMA_VERSION,
        "code": {
            key: value["code"].get(key)
            for key in ("source", "commit_sha", "side", "status")
        },
        "repository_rules": {
            key: value["repository_rules"].get(key)
            for key in ("source", "commit_sha", "status")
        },
        "missing": value["missing"],
        "truncated": value["truncated"],
    }
