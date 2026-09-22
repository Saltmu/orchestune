"""Jev-based review finding filtering (PoC).

Evaluates AI review inline findings with Jev, determining validity and impact,
and filters out low-impact or low-validity findings based on a simple threshold.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_JEV_API_URL = "https://api.jev.ai/v1/evaluate"
DEFAULT_VALIDITY_THRESHOLD = 0.7


@dataclass(frozen=True)
class JevFindingEvaluation:
    """Evaluation result for an inline review finding."""

    validity: float
    impact: str
    bypassed: bool = False
    raw_response: dict[str, Any] = field(default_factory=dict, repr=False)


def is_finding_accepted(
    validity: float,
    impact: str,
    threshold: float = DEFAULT_VALIDITY_THRESHOLD,
) -> bool:
    """Check if a finding satisfies acceptance threshold.

    Rules:
    - impact must not be LOW (case-insensitive)
    - validity must be greater than or equal to threshold
    """
    impact_norm = (impact or "").strip().upper()
    if impact_norm == "LOW":
        return False
    return validity >= threshold


def evaluate_finding_with_jev(
    comment: str,
    path: str = "",
    line: Any = "",
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
) -> JevFindingEvaluation:
    """Evaluate a single review finding with the Jev API.

    API key is read from JEV_API_KEY environment variable if not explicitly passed.
    If no key is configured, evaluation is bypassed safely without raising errors.
    """
    resolved_key = api_key or os.environ.get("JEV_API_KEY")
    if not resolved_key:
        return JevFindingEvaluation(
            validity=1.0,
            impact="HIGH",
            bypassed=True,
        )

    url = base_url or os.environ.get("JEV_API_URL") or DEFAULT_JEV_API_URL
    payload = json.dumps(
        {
            "comment": comment,
            "path": path,
            "line": line,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            validity = float(data.get("validity", 1.0))
            impact = str(data.get("impact", "HIGH")).upper()
            return JevFindingEvaluation(
                validity=validity,
                impact=impact,
                bypassed=False,
                raw_response=data,
            )
    except Exception as exc:
        # Safe fallback: do not leak API key, log sanitized error type and proceed
        print(
            f"Warning: Jev evaluation failed ({type(exc).__name__}); bypassing filter.",
            file=sys.stderr,
        )
        return JevFindingEvaluation(
            validity=1.0,
            impact="HIGH",
            bypassed=True,
        )


def filter_review_findings(
    inline_comments: list[dict[str, Any]],
    bot_name: str = "claude",
    threshold: float | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Filter inline comments using Jev evaluation and threshold policy.

    Outputs structured evaluation logs to stderr for PoC visibility.
    If JEV_API_KEY is not set, findings pass through untouched.
    """
    resolved_key = api_key or os.environ.get("JEV_API_KEY")
    if not resolved_key:
        return inline_comments

    if threshold is None:
        try:
            threshold = float(
                os.environ.get("JEV_THRESHOLD", DEFAULT_VALIDITY_THRESHOLD)
            )
        except ValueError:
            threshold = DEFAULT_VALIDITY_THRESHOLD

    accepted_findings: list[dict[str, Any]] = []

    for item in inline_comments:
        comment = str(item.get("body") or "")
        path = str(item.get("path") or "")
        line = item.get("line") or ""

        evaluation = evaluate_finding_with_jev(
            comment=comment,
            path=path,
            line=line,
            api_key=resolved_key,
            base_url=base_url,
        )

        accepted = is_finding_accepted(
            validity=evaluation.validity,
            impact=evaluation.impact,
            threshold=threshold,
        )

        log_entry = {
            "reviewer": bot_name,
            "comment": comment,
            "path": path,
            "line": line,
            "validity": evaluation.validity,
            "impact": evaluation.impact,
            "accepted": accepted,
        }
        print(json.dumps(log_entry), file=sys.stderr)

        if accepted:
            accepted_findings.append(item)

    return accepted_findings
