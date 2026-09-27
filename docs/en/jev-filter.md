# Evaluating review findings with Jev

`wait_for_review.py` evaluates inline findings only when `JEV_API_KEY` is configured and findings exist. Without a key, it preserves findings without fetching additional PR metadata or code, or calling Jev. Immediate, polling, and offline `--review-state-file` results use the same acceptance policy.

## Evaluation and acceptance

Each finding uses one request containing validity (quality of the stated evidence), impact (consequences if the defect occurs), and applicability to current use. Existing API URLs, exponential backoff, and retry limits are retained.

| applicability | Meaning |
| --- | --- |
| APPLICABLE | A defect occurs under current/documented conditions, or concretely violates an existing requirement or repository rule |
| SPECULATIVE | Code and execution evidence establish that undocumented assumptions are necessary and existing requirements are not violated |
| UNKNOWN | Relevant code, input provenance, or rules are missing, contradictory, or materially truncated |

Evaluation failures set `bypassed=True` and always retain findings, even for thresholds above 1. Only SPECULATIVE with confidence at least 0.9, consistent code/execution provenance, available base rules, and empty missing/truncated lists can be excluded by the new axis. Confidence is not a calibrated probability of occurrence. Path classification, PR claims of internal use or YAGNI, and low frequency alone cannot establish speculation.

Otherwise the existing `validity >= threshold` and `impact != LOW` policy applies. Legacy responses and invalid new answers become UNKNOWN; invalid legacy answers bypass the entire evaluation.

## Transmitted context (schema_version 2)

The existing `state.comment/path/line` fields remain. `state.context` adds:

- `pr`: number, title, body, head_sha, base_sha; fetched once per processing unit.
- `code`: source (git_blob / review_diff), commit_sha, side, start_line, text, status; ten lines around the finding from the matching local blob, plus a separate module_description.
- `execution`: component_hint, input_trust, evidence. `scripts/` produces only internal_tool_candidate; input_trust stays unknown. Jev assesses the meaning of execution evidence.
- `repository_rules`: `.agents/AGENTS.md` from the PR base, source, commit_sha, text, status. PR claims do not invalidate existing rules.
- `missing/truncated`: unavailable data, invalid provenance, and omissions. Conservatively, any truncation prevents exclusion by applicability.

Inline normalization preserves id, diff_hunk, side, start_line, start_side, commit_id, original_commit_id, and original_line. Display line and actual position_line are separate. If comment commit_id matches the PR head, RIGHT uses head and LEFT uses the unique locally verified merge base of head/base, which may differ from the base branch tip. An unavailable or ambiguous merge base falls back to the review diff. Outdated RIGHT comments use their original commit and original_line. Unprovable LEFT or stale mappings fall back to the original review diff, never current working tree lines.

Git commands use validated full SHAs and relative paths as argument lists. Absolute paths, traversal, invalid SHAs, binary/non-UTF-8 blobs, and oversized files are rejected. Missing local objects never trigger fetch or checkout. Review diffs are the fallback; absent snippets yield code.status=missing. Imports and callers are not recursively explored.

Limits: comment 4,000 characters, PR title 300, body 3,000, code 6,000, module description 2,000, rules 4,000, raw blob 128 KiB, and entire request 32 KiB UTF-8. Oversized payloads shrink module description, execution evidence, PR body, rules, code, comment, then path, preserving character boundaries and truncation markers.

Offline input may supply a shared top-level optional `context` or a per-inline `context`, which takes precedence. The same provenance-bearing format is required; omitted context is unknown. Offline evaluation performs no additional network or Git acquisition. Instructions explicitly treat embedded commands in context as evaluation data.

## Logs and validation limits

Existing JSONL keys remain, with schema_version, applicability, applicability_confidence, decision_reason (bypass / speculative / low_validity / low_impact / accepted), and context source SHAs, status, missing, and truncated markers added. stderr includes the same decision information. New log fields do not persist code, full PR/rule text, or API keys. Context acquisition failures appear as missing data; evaluation failures are bypassed.

Contrast examples cover documented trusted internal input, external input, destructive internal effects, rule violations, and insufficient information. #1086 remains a disputed example with no universal SPECULATIVE expectation. Mock-based unit and integration tests validate decisions and contracts, not actual Jev accuracy. Any live comparison must separately record the model, examples, results, and false exclusions of findings that should remain.
