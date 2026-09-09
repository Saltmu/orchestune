"""Durable ordinary-worker launch journal, separate from quota reservations."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Literal

from orchestune.forge import Forge
from orchestune.validation import validate_ref_name

MARKER = "<!-- orchestune:launch-attempt -->"
_BLOCK = re.compile(re.escape(MARKER) + r"\s*```json\s*\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class LaunchAttempt:
    attempt_id: str
    phase: Literal["prepared", "unknown", "launched"]
    target: str
    branch: str
    base_branch: str
    started_at: float
    external_id: str | None = None
    external_url: str | None = None


def attempt_from_body(body: str) -> LaunchAttempt | None:
    """Malformed/duplicate records are uncertainty, never permission to launch."""
    body = body.replace("\r\n", "\n")
    if MARKER not in body:
        return None
    matches = list(_BLOCK.finditer(body))
    if body.count(MARKER) != 1 or len(matches) != 1:
        raise ValueError("ambiguous launch-attempt journal")
    data = json.loads(matches[0].group(1))
    if not isinstance(data, dict):
        raise ValueError("invalid launch-attempt journal")
    required = ("attempt_id", "target", "branch", "base_branch")
    if any(not isinstance(data.get(k), str) or not data[k] for k in required):
        raise ValueError("missing launch-attempt identity")
    if data.get("phase") not in {"prepared", "unknown", "launched"}:
        raise ValueError("invalid launch-attempt phase")
    validate_ref_name(data["branch"])
    validate_ref_name(data["base_branch"])
    started = data.get("started_at")
    if (
        isinstance(started, bool)
        or not isinstance(started, int | float)
        or not math.isfinite(started)
    ):
        raise ValueError("invalid launch-attempt timestamp")
    for key in ("external_id", "external_url"):
        if data.get(key) is not None and (
            not isinstance(data[key], str) or not data[key]
        ):
            raise ValueError("invalid launch-attempt handle")
    if data["phase"] == "launched" and not data.get("external_id"):
        raise ValueError("launched attempt has no handle")
    try:
        return LaunchAttempt(**data)
    except TypeError as exc:
        raise ValueError("invalid launch-attempt fields") from exc


def read_attempt(forge: Forge, issue_number: int) -> LaunchAttempt | None:
    issue = forge.get_issue(issue_number)
    if issue is None:
        raise ValueError(f"cannot read launch-attempt issue #{issue_number}")
    return attempt_from_body(issue.body)


def write_attempt(
    forge: Forge,
    issue_number: int,
    attempt: LaunchAttempt,
    *,
    expected: LaunchAttempt | None,
) -> None:
    """Preserve current prose and reject changed journals under the runner lock.

    This is not a cross-runner CAS; independent dispatchers must not own the same
    task concurrently. A failed/ambiguous write never authorizes a provider call.
    """
    issue = forge.get_issue(issue_number)
    if issue is None or attempt_from_body(issue.body) != expected:
        raise ValueError("launch-attempt journal changed or issue unavailable")
    body = issue.body.replace("\r\n", "\n")
    block = (
        MARKER
        + "\n```json\n"
        + json.dumps(asdict(attempt), ensure_ascii=False)
        + "\n```"
    )
    updated = (
        _BLOCK.sub(lambda _: block, body) if expected else body + "\n\n" + block + "\n"
    )
    forge.update_issue_body(issue_number, updated)
    if read_attempt(forge, issue_number) != attempt:
        raise ValueError("launch-attempt journal write could not be verified")
