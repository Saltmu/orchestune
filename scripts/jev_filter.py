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
DEFAULT_JEV_API_URL = f"{DEFAULT_JEV_BASE_URL}/evaluate"
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
    """Evaluate a single review finding with the Jev API.

    API key is read from JEV_API_KEY environment variable if not explicitly passed.
    Base URL or endpoint can be passed via base_url argument or JEV_BASE_URL / JEV_API_URL env vars.
    If no key is configured, evaluation is bypassed safely without raising errors.
    Applies comment chunking/truncation and exponential backoff retry for transient errors.
    """
    resolved_key = api_key or os.environ.get("JEV_API_KEY")
    if not resolved_key:
        return JevFindingEvaluation(
            validity=1.0,
            impact="HIGH",
            bypassed=True,
        )

    if base_url:
        cleaned = base_url.rstrip("/")
        url = cleaned if cleaned.endswith("/evaluate") else f"{cleaned}/evaluate"
    elif os.environ.get("JEV_BASE_URL"):
        cleaned = os.environ["JEV_BASE_URL"].rstrip("/")
        url = cleaned if cleaned.endswith("/evaluate") else f"{cleaned}/evaluate"
    elif os.environ.get("JEV_API_URL"):
        url = os.environ["JEV_API_URL"]
    else:
        url = DEFAULT_JEV_API_URL

    # Chunk / truncate oversized comments to prevent resource bloat and comply with API guidelines
    comment_text = comment or ""
    if len(comment_text) > MAX_COMMENT_LENGTH:
        chunked_comment = (
            comment_text[:MAX_COMMENT_LENGTH] + "\n... [truncated for Jev evaluation]"
        )
    else:
        chunked_comment = comment_text

    payload = json.dumps(
        {
            "comment": chunked_comment,
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

    backoff = initial_backoff
    for attempt in range(max_retries + 1):
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
        except urllib.error.HTTPError as exc:
            # 429 (rate limit) or 5xx (server error) are transient -> retry with backoff
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
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
