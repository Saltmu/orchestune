# Code-analysis observation (#822)

## Applicability and storage
During [#822](https://github.com/Saltmu/orchestune/issues/822) observation, record the cohort's start date, developer environment, inclusion criteria and ledger location in #822 before collecting data.
If absent, propose these in the current record; do not silently infer historical coverage.
Include every PR in that environment and period, including docs-only, tool-unused, fallback, zero-finding, and unsuccessful PRs. Log exclusion reasons; count only reviewed PRs toward the 5/20 milestones. Missing reviews are not zero findings.

Keep the working record in implementation_plan.md, then the PR body under `#822 observation`.
Before production edits, preserve the original scope table in an immutable task-Issue comment or a committed, tracked observation artifact and save its permalink in the plan.
Never rely on ignored implementation_plan.md alone or rewrite the snapshot; append corrections elsewhere.
External writes require task authorization. Without a durable store, pause observed work, hand off the pending snapshot and retain its worktree.
The #822 ledger links each PR once with status and its detailed record; update it on retries.

## Capture at the time of work
- Before preparation/search: record environment, tool/version and intended use; measure
  preparation, index updates, and impact investigation separately with start/end times,
  elapsed duration and method. Separate waiting from active work if measurable.
- Record actual tool calls/use and fallback reason, branch/base SHA and index correspondence
  evidence. Tokens need an available counter, source and scope; unavailable values stay
  `unavailable (reason)`, never estimates or zero. Shared setup costs are recorded once
  and linked by later PRs, not repeatedly charged.
- Immediately before the first review request, save head SHA, reviewer/provider/model when available, initial diff size/base and change type. Keep initial values after later pushes.
- After each round, record the actual reviewed SHA when verifiable, evidence links and
  reason for the next round: findings, execution error, base update, duplicate, or other.
  A requested SHA is not proof of the SHA actually reviewed; mark uncertainty explicitly.

## Findings and attribution
Count independent reported findings, not confirmed bugs or only applied fixes. Assign one primary category:
A = direct propagation omission at a consumer/alternate path; B = contract/spec mismatch
between related processing; C = state/order/recovery; D = local value/input/environment.
Exclude pure readability, reuse or performance suggestions. Retain an exclusion reason.
Link canonical finding/thread evidence, merge reposts and summary copies, and distinguish
initial findings from new findings introduced by fixes. Mark unclear origin as unknown.
Acceptance-criteria in/out-of-scope decisions remain separate from these categories.

For each A, compare the preserved table: absent = enumeration miss; wrongly excluded = classification miss; in scope but unchanged = implementation omission.
Record evidence and uncertainty; a missing snapshot is unknown. Enumeration misses do not prove tool failure: record actual tool and supplementary-search use.

## Record template (fill at the stages above)
| Field | Value / evidence |
| :--- | :--- |
| PR, cohort, status, inclusion/exclusion | |
| Environment, tool/version, actual use, fallback, index/base SHA | |
| Preparation / update / investigation times; method; shared setup link | |
| Tokens: value, source, scope or unavailable reason | |
| Original scope snapshot; appended reconciliation | |
| First requested / actual reviewed SHA; reviewer/provider/model | |
| Initial files / additions / deletions / base; change type | |
| Findings: canonical link, A–D/excluded, origin, A attribution/evidence | |
| Rounds: SHA, evidence, next-round reason | |
| Counts A/B/C/D (explicit zero only when reviewed); unresolved items | |
| Test results / post-integration defects or not yet observed | |

## Evaluation and completion
Prepare an interim assessment at 5 reviewed PRs and a continuation decision at 20 or more as a guide.
Both follow the storage/authorization rule above: publish in #822 when authorized and available;
otherwise retain the assessment locally and hand off the pending publication; never skip preparation.
Report new A findings/PR and fraction of PRs with A; aggregate B separately and distinguish initial from fix-induced findings.
Compare similar reviewer, provider, size, type and actual-use conditions against #822's baseline, showing missing data and denominators. Do not equate one finding with one review round saved.
Use individual attribution and cases as the main evidence alongside measured overhead;
20 PRs do not establish statistical significance or causality. Check tests and later
defects for possible weaker review. Record continue, improve, observe more, or withdraw,
with reasons and links; no preset reduction target is required.
Protocol/setup completion closes only its own child Issue (`Closes #855`, `Refs #822`
for this change). Do not emit a done outcome for #822 while its observation remains open.
