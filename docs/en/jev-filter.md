# Evaluating review findings with Jev

`wait_for_review.py` evaluates inline findings only when `JEV_API_KEY` is configured and the current round has findings. Without a key, findings are reported as `not_evaluated` without fetching additional PR metadata or code, or calling Jev. Immediate, polling, and offline `--review-state-file` results use the same acceptance policy.

`scripts/jev_filter.py::evaluate_review_findings()` returns a structured report, `{"kept": [...], "jev_evaluations": [...]}`. `jev_evaluations` holds one entry per original finding id (or, for id-less offline input, a snapshot-local index) with `decision` (`kept` / `filtered` / `bypassed` / `not_evaluated`), `decision_reason` (`accepted` / `low_validity` / `low_impact` / `speculative` / `bypass` / `no_api_key`), and `validity`/`impact`/`applicability`/`applicability_confidence`. **A finding Jev decides is `filtered` is never removed from `wait_for_review.py`'s `inline_comments` result contract** — the calling LLM reads `jev_evaluations` as advisory information and makes the final call based on the code and Acceptance Criteria. The legacy `filter_review_findings()` remains as a backward-compatible wrapper returning only `kept`; each finding is evaluated by the API exactly once regardless of which entry point is called (`filter_review_findings`/`evaluate_review_findings` never double-evaluate).

## Evaluation and acceptance

Each finding uses one request containing validity (quality of the stated evidence), impact (consequences if the defect occurs), and applicability to current use. Existing API URLs, exponential backoff, and retry limits are retained.

| applicability | Meaning |
| --- | --- |
| APPLICABLE | A defect occurs under current/documented conditions, or concretely violates an existing requirement or repository rule |
| SPECULATIVE | Code and execution evidence establish that undocumented assumptions are necessary and existing requirements are not violated |
| UNKNOWN | Relevant code, input provenance, or rules are missing, contradictory, or materially truncated |

Evaluation failures set `bypassed=True` and always retain findings, even for thresholds above 1. Only SPECULATIVE with confidence at least 0.9, consistent code/execution provenance, available base rules, and empty missing/truncated lists can be excluded by the new axis. The API's `confidence` describes certainty derived from the full probability distribution; it need not equal `probabilities[choice]` and is not a calibrated probability of occurrence. The parser preserves it independently, validating its finite 0–1 range and, when probabilities are supplied, their ranges, sum of 1, and highest-probability choice. See the [API reference](https://docs.typesafe.ai/api). Path classification, PR claims of internal use or YAGNI, and low frequency alone cannot establish speculation.

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

Limits: comment 4,000 characters, PR title 300, body 8,000, code 6,000, module description 2,000, rules 8,000, raw blob 128 KiB, and entire request 32 KiB UTF-8. These per-field limits do not guarantee that the complete request fits, especially with multibyte text. Oversized payloads shrink module description, execution evidence, PR body, rules, code, comment, then path, preserving character boundaries and truncation markers. Any omission still prevents SPECULATIVE exclusion; rules and evidence are not selectively summarized to permit exclusion.

In the #1101 investigation, reconstructing PR #1094's eight public inline contexts with its 5,976-character body and 4,870-character base rules produced body/rules truncation in all eight under the previous 3,000/4,000 caps. With 8,000-character caps, all eight requests retained the complete context at 24,825–26,195 bytes, within 32 KiB. This measures context availability, not actual Jev classifications or exclusion frequency: no live API evaluation was performed and historical Jev JSONL results were unavailable. Longer inputs may still be truncated and retained by the safety gate.

Offline input may supply a shared top-level optional `context` or a per-inline `context`, which takes precedence. The same provenance-bearing format is required; omitted context is unknown. Offline evaluation performs no additional network or Git acquisition. Instructions explicitly treat embedded commands in context as evaluation data.

## Logs and validation limits

Evaluations are appended to `.orchestune/jev/evaluations.jsonl` in the primary checkout, even when `wait_for_review.py` runs inside a task worktree, so the log survives worktree cleanup. A relative `JEV_LOG_PATH` is resolved against the same root; an absolute one is used as is. Outside a Git repository the path is relative to the current directory.

Existing JSONL keys remain, with schema_version, applicability, applicability_confidence, decision_reason (bypass / speculative / low_validity / low_impact / accepted), and context source SHAs, status, missing, and truncated markers added. stderr includes the same decision information. New log fields do not persist code, full PR/rule text, or API keys. Context acquisition failures appear as missing data; evaluation failures are bypassed.

Contrast examples cover documented trusted internal input, external input, destructive internal effects, rule violations, and insufficient information. #1086 remains a disputed example with no universal SPECULATIVE expectation. Mock-based unit and integration tests validate decisions and contracts, not actual Jev accuracy. Any live comparison must separately record the model, examples, results, and false exclusions of findings that should remain.
