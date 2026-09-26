"""Jev-based review finding filtering (PoC).

Evaluates AI review inline findings with Jev, determining validity and impact,
and filters out low-impact or low-validity findings based on a simple threshold.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_JEV_API_URL = f"{DEFAULT_JEV_BASE_URL}/systemone"
DEFAULT_VALIDITY_THRESHOLD = 0.7
MAX_COMMENT_LENGTH = 4000
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 0.5


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


def _resolve_api_url(base_url: str | None) -> str:
    configured_base = base_url or os.environ.get("JEV_BASE_URL")
    if configured_base:
        cleaned = configured_base.rstrip("/")
        return cleaned if cleaned.endswith("/systemone") else f"{cleaned}/systemone"
    return os.environ.get("JEV_API_URL") or DEFAULT_JEV_API_URL


def _build_payload(comment: str, path: str, line: Any) -> bytes:
    comment_text = comment or ""
    if len(comment_text) > MAX_COMMENT_LENGTH:
        comment_text = (
            comment_text[:MAX_COMMENT_LENGTH] + "\n... [truncated for Jev evaluation]"
        )
    return json.dumps(
        {
            "model": "jev-latest",
            "state": {"comment": comment_text, "path": path, "line": line},
            "questions": {
                "validity": {
                    "type": "noul",
                    "instructions": (
                        "Does the review finding in `comment`, at `path` and `line`, "
                        "describe a concrete, plausible defect that needs fixing, "
                        "rather than a speculative edge case or stylistic preference?"
                    ),
                },
                "impact": {
                    "type": "choice",
                    "instructions": (
                        "What is the impact of the defect described in `comment` "
                        "at `path` and `line`, if it occurs?"
                    ),
                    "criteria": {
                        "LOW": "Cosmetic, stylistic, or negligible functional impact.",
                        "MEDIUM": "A functional defect affecting a limited use case.",
                        "HIGH": "Major correctness, security, or availability failure.",
                    },
                },
            },
        }
    ).encode("utf-8")


def _parse_evaluation(data: dict[str, Any]) -> JevFindingEvaluation:
    validity_answer = data["answers"]["validity"]
    impact_answer = data["answers"]["impact"]
    validity = validity_answer["noul"]
    impact = impact_answer["choice"]
    if (
        validity_answer["type"] != "noul"
        or impact_answer["type"] != "choice"
        or isinstance(validity, bool)
        or not isinstance(validity, int | float)
        or not 0.0 <= validity <= 1.0
        or impact not in ("LOW", "MEDIUM", "HIGH")
    ):
        raise ValueError("Invalid Jev answers")
    return JevFindingEvaluation(
        validity=float(validity), impact=impact, raw_response=data
    )


def _evaluate_request(
    req: urllib.request.Request,
    timeout: float,
    max_retries: int,
    initial_backoff: float,
) -> JevFindingEvaluation:
    backoff = initial_backoff
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _parse_evaluation(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504, 529) and attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            print(
                f"Warning: Jev evaluation HTTP error {exc.code}; bypassing filter.",
                file=sys.stderr,
            )
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            print(
                f"Warning: Jev evaluation network error ({type(exc).__name__}); bypassing filter.",
                file=sys.stderr,
            )
            break
        except Exception as exc:
            print(
                f"Warning: Jev evaluation failed ({type(exc).__name__}); bypassing filter.",
                file=sys.stderr,
            )
            break
    return JevFindingEvaluation(validity=1.0, impact="HIGH", bypassed=True)


def evaluate_finding_with_jev(
    comment: str,
    path: str = "",
    line: Any = "",
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
    max_retries: int = MAX_RETRIES,
    initial_backoff: float = INITIAL_BACKOFF_SECONDS,
) -> JevFindingEvaluation:
    """Evaluate a review finding via TypeSafe's System One API.

    API key defaults to JEV_API_KEY. The URL defaults to /v1/systemone;
    base_url or JEV_BASE_URL selects a base or /systemone endpoint, while
    JEV_API_URL specifies an exact endpoint. Explicit base_url takes precedence.
    Missing keys or failed evaluations bypass filtering to preserve findings.
    Oversized comments are truncated; transient errors use bounded backoff.
    """
    resolved_key = api_key or os.environ.get("JEV_API_KEY")
    if not resolved_key:
        return JevFindingEvaluation(validity=1.0, impact="HIGH", bypassed=True)
    req = urllib.request.Request(
        _resolve_api_url(base_url),
        data=_build_payload(comment, path, line),
        headers={
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return _evaluate_request(req, timeout, max_retries, initial_backoff)


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
