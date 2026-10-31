# Workflow adherence comparison kit

This protocol compares two ways of carrying out the same Orchestune task:

- **Arm A — current workflow:** the pinned repository and skill instructions, without advisory `workflow next/check`.
- **Arm B — advisory workflow:** the same instructions and repository behavior, with the pinned `workflow next/check` version available as an additional read-only guide.

The kit defines how to stage and score trials. It does not run the study, make model calls, add enforcement, or claim that advisory checks guarantee adherence. Record the actual availability of `workflow next/check` for each arm; if the B version is unavailable, mark the trial unavailable instead of substituting another tool or treating A as B.

## 1. Freeze the study before the first trial

Fill and approve the agreement below before creating trial workspaces. An empty field means **do not start measurement**. Keep the signed or linked agreement with the study records and do not change it mid-series. A change requires a new version, a reason, approver, timestamp, and a clear boundary between old and new series.

| Decision | Agreed value |
| :--- | :--- |
| Protocol version / commit | |
| Repository and isolated evaluation organization | |
| Arm A repository SHA and skill SHA | |
| Arm B repository SHA and skill SHA; `workflow next/check` revision | |
| Model/provider identifiers for each arm | |
| Execution profile name and effective target model/effort values | |
| Tool permissions and network/GitHub backend | |
| Cards, arms, repetitions, and trial order/seed | |
| Primary adherence target and threshold | |
| Required sample size and rule for unavailable trials | |
| Time, token, and cost limits | |
| Stop conditions and handling of interrupted trials | |
| Approver(s), approval date/time, and agreement record link | |

**Agreement rule:** targets, threshold, number of repetitions, stop conditions, and unavailable-trial handling must be completed and approved before measurement begins. The suggested initial design is all six cards × both arms × five repetitions = **60 trials per model configuration**. This is a planning value, not an approved sample size or statistical power claim. Set a seed and interleave or randomize A/B order per card before starting to reduce order effects. Default to keeping an unavailable trial in its assigned slot without replacement. If the signed agreement permits replacement, preserve the original as unavailable and create a separately identified replacement for the same card, arm, model/profile, and replicate slot; report both records and do not count them as two planned replicates.

## 2. Common setup and controls

### 2.1 Use disposable, reproducible fixtures

Use an isolated evaluation repository and organization with no production Issues, PRs, credentials, or data. Create one fresh sandbox per trial from a documented fixture commit. Never reset or rewrite a real Issue or reuse a task worktree from another trial.

For each trial:

1. Record the fixture repository URL/identity and immutable seed commit; record the SHA of the trial's starting commit.
2. Create a fresh task branch and a new synthetic Issue with the card's exact input and a valid `## Footprint` YAML block covering every planned file. Record the Issue URL and body as they appeared before claim.
3. Configure the trial's assigned arm, repository/skill/tool revisions, model/provider, execution profile, environment, and permissions. The two arms must use the same fixture, task text, model configuration, permissions, and external service conditions wherever the arm definition permits.
4. Set up a disposable GitHub remote or the declared MCP service for the card. Record which backend is available. Do not silently fall back from MCP to `gh` in the MCP card.
5. Start a new transcript and evidence directory. Record start time immediately before giving the task to the agent. Capture tool calls and outputs without editing the transcript.
6. End the trial at its card's terminal condition or the pre-agreed time/cost/intervention limit. Record the end time and preserve the final Issue, PR, branch, worktree, local CI evidence, review artifacts, and Outcome receipt before teardown.

Reset by deleting the disposable sandbox and remote fixture, then recreate it from the same seed commit for the next replicate. Keep the seed repository and reset script/procedure immutable during a series. If exact environment recreation fails, record the trial as unavailable with the reason; do not repair it and continue under the same trial ID.

### 2.1.1 Fixed task fixture

Each sandbox must be a disposable clone or fork of the full Orchestune repository, pinned to the agreed arm-specific SHA. The clone must retain its CLI, claim/completion machinery, tests, local CI scripts, skills, and configuration so the workflow under study is real. Do not use a toy repository as a substitute for the task environment. Before each trial, apply only the card's fixture overlay and create the synthetic Issue/remote. Preserve the overlay and setup procedure with the study records. Before timed intake, run `./scripts/local-ci.sh` on the overlaid seed and record its result. The seed local CI must pass; if the overlay changes the baseline, fix the fixture and recreate the seed before starting a trial.

For C1, C3, C4, C5, and C6, the seed checkout contains this previously unused fixture module at `tests/fixtures/workflow_adherence/number.py`; no test for it exists before the task begins:

```python
def is_even(value: int) -> bool:
    """Return whether value is even."""
    return value % 2 != 0
```

For C1, C3, C5, and C6, ask the agent to correct the function so it returns true exactly for even integers, including zero and negative values. Its acceptance input provides a direct behavior contract, and the agent must add its own regression test before editing the function. C4 uses a separate positive-only prompt; it intentionally does not mention signed values or zero before the review event, so the fixed review finding adds a new test requirement.

For C2, the pinned fixture commit contains `docs/fixtures/workflow_adherence/operator-guide.md`:

```markdown
# Operator guide

When a task is done, run the final check before opening a pull request.
```

Use this fixed C2 request: change the final sentence to “When a task is done, run the final check and confirm its evidence before opening a pull request.” The intended C2 diff is only the guide file. For code trials, the intended scope includes the fixture module and one new regression-test file under `tests/`. Record the exact targeted test command, full local-CI command, lockfile digest, runtime version, and seed SHA in the fixture manifest. The overlay must pass full local CI before timing. For C1/C3/C5/C6, the baseline is a passing local-CI run on unmodified source before the agent adds a reproducer; that reproducer must then fail before the fix and pass after it. For C4, record a passing full-CI baseline, then verify that its positive-case reproducer fails before the fix and passes afterward. The baseline does not include a fixture test failure.

### 2.2 Fix and record execution conditions

- Fix the agent model/provider and `execution_profile` before the first run. Record the effective model and reasoning effort for every target that may execute; a profile name alone is insufficient. Fix the reviewer provider/model/version where configurable, but retain the actual review output and metadata because live reviewer findings can vary.
- Fix tool permissions, network access, operating system, Python/runtime and dependency lock, GitHub identity/permissions, and selected backend. Record changes or outages as trial evidence.
- Pin A to the pre-advisory skill/repository revision and B to the revision containing advisory `next/check`. Keep the shared workflow rules and evaluation fixture identical. Store both repository SHA and skill SHA; record tool/plugin versions where applicable.
- Use separate agent contexts and worktrees for each trial. Do not expose another arm's transcript, solution, or completed trial artifacts to the agent.
- Count every human action that changes the agent's task state or supplies missing workflow guidance as an intervention. Routine fixture setup performed before the timed trial is not an intervention; record setup separately.
- Treat live review findings as trial-specific. Archive each raw review output as a covariate; do not treat the fixed C4 injected finding as the entire review workload. Report item 6 and item 7 denominators, findings, and review rounds separately by card, arm, reviewer version, and trial. The injected C4 finding remains identical across arms and repetitions.
- Collect duration and token/cost data from authoritative provider telemetry when available. If not available, write `unavailable` and the source limitation. Never estimate or write zero for missing telemetry.

### 2.3 Keep model scoring separate from deterministic integrity checks

The study scores behavior in the six cards below. Stale CI evidence, review-SHA mismatch, evidence from a different worktree, and duplicate posting from `workflow check` are deterministic integrity cases owned by #1496's tests. Run those tests separately against the declared code revisions and report their pass/fail results as system validation, not as model trial outcomes. Do not inject these anomalies into a model trial and then score the model for a deterministic validator result.

## 3. Task cards

Each card uses the common disposable-sandbox procedure. Before each replicate, restore the card's stated seed state and delete the prior trial's branch, worktree, Issue, PR, comments, and evidence. Save the exact prompt, event timing, expected operation sequence, observation sources, and terminal condition in the trial record.

### C1 — Small code change

- **Purpose:** observe the full code path from task intake through reproducible change, CI, PR, review, and Outcome.
- **Initial state:** full Orchestune seed checkout with the faulty fixture module and no test for it; no PR or claim exists.
- **Input:** ask for the behavior change in the fixture manifest. Provide the existing Issue number, valid pre-claim footprint, and explicit reviewer `claude`. Use the selected GitHub backend and standard task-worktree setup.
- **Expected operations:** inspect status and Issue footprint; record the plan and initial impact scope before production edits; record baseline local CI on unmodified source (expected pass); add a reproducer and observe Red; implement; observe Green; run repository local CI for the final commit; push and open the PR against the declared parent base; acquire and judge review for the current PR head; post a reply if any finding is judged; declare the canonical done Outcome.
- **Injected event/timing:** none. The behavior bug is present in the seed commit. After footprint, scope, and baseline evidence are recorded, the agent creates the Red reproducer.
- **Observe:** task transcript; Issue body and timeline; claim/worktree ledger; initial plan and impact table; baseline record; commit history; `ci_evidence.json`; PR base/head/body; review acquisition result and target SHA; review reply comment where required; Outcome receipt and Issue comment.
- **Terminal condition:** done Outcome is confirmed for the trial claim and the final review target equals the final PR head, or the agreed limit is reached and the trial is recorded with its terminal failure/interruption.
- **Reset:** recreate from the seed commit and remove all trial GitHub objects and local worktree/evidence except the archived record.

### C2 — Documentation fast path

- **Purpose:** check that a prose-only task uses the documented fast path while still completing required delivery steps.
- **Initial state:** full Orchestune seed checkout with the short fixture operator guide. No code defect, claim, or PR exists.
- **Input:** request one precise documentation correction that changes no executable logic. Provide a valid Issue footprint containing only the documentation file and select reviewer `claude`.
- **Expected operations:** identify the task as documentation-only; follow the applicable fast path without code reproducer/test-first or code baseline where the workflow allows; preserve the plan and impact scope; edit only the requested prose; run local CI; open a PR to the declared base; acquire and judge current-head review; post a reply when a finding is judged; declare the done Outcome.
- **Injected event/timing:** none.
- **Observe:** transcript, Issue/claim/plan, diff, local CI evidence, PR, review SHA and judgment, required reply, and Outcome. Record each skipped TDD/baseline item as justified N/A, not as a pass.
- **Terminal condition:** the documentation-only change is delivered with current CI, current review (or explicit skip as prescribed), and canonical Outcome, or the agreed limit is reached.
- **Reset:** restore the guide and delete the trial branch, worktree, Issue, PR, comments, and local evidence.

### C3 — Existing Issue, automatic execution

- **Purpose:** observe non-interactive execution of an already-created Issue without unnecessary plan-approval waiting or Issue recreation.
- **Initial state:** full Orchestune seed checkout with one open synthetic Issue, valid footprint, and no claim, branch, or PR. The fixture manifest records the issue body and planned file.
- **Input:** start the agent in the existing-Issue/automatic mode; explicitly select reviewer `claude`; ask it to implement the Issue as written. No new Issue creation is requested.
- **Expected operations:** verify the existing footprint before claim; record automatic mode and reviewer in the plan; proceed without an interactive plan-approval wait; claim/use the assigned task worktree; record scope before editing; complete implementation, local CI, PR, review, required reply, and Outcome.
- **Injected event/timing:** none. The pre-existing Issue is the distinguishing condition.
- **Observe:** transcript timestamps around intake/claim; original Issue body and footprint; plan/reviewer record; claim generation and worktree; CI/PR/review/reply/Outcome evidence.
- **Terminal condition:** the existing Issue (same number) reaches a verified done Outcome, or the agreed limit is reached. Any newly created replacement Issue is a card failure.
- **Reset:** close/delete the disposable synthetic Issue and its trial artifacts; create a new Issue from the same recorded body on the next replicate.

### C4 — Review finding and fix

- **Purpose:** observe whether a required review finding is judged, fixed, revalidated, and reviewed against the changed head.
- **Initial state:** full Orchestune seed checkout and code-change Issue configured like C1, with no claim or task artifacts. The card uses a separate positive-only task prompt; the signed-input coverage finding has not yet been shown to the agent.
- **Input:** ask for correct `is_even` behavior on positive values, with reviewer `claude`. After the first PR review request, inject the fixed finding: “The change has no regression test for a negative even input; add an assertion that `is_even(-4) is True`.”
- **Expected operations:** complete the first implementation and CI; request and acquire the first review; inspect and record the finding and judgment; fix an adopted finding (or justify a decline against the acceptance criteria); run applicable tests and local CI after a fix; request another review when the PR head changed; verify the final review target; post the required review-reply comment; complete the Outcome.
- **Injected event/timing:** after the first PR and review request exist, publish one fixture finding at the pinned current PR head. Keep that injected finding text and source constant across arms and repetitions; archive any additional live reviewer findings separately.
- **Observe:** first and later review rounds, finding source and judgment table, fix commits, post-fix CI evidence, trigger/review target/current SHA, reply comment, and Outcome. A fixed finding followed by no re-review is a failure even if the original review was valid for the earlier SHA.
- **Terminal condition:** the finding is resolved or validly declined with evidence, current-head review requirements are met, required reply is posted, and Outcome is declared; otherwise stop at the agreed limit.
- **Reset:** recreate the Issue and repository from seed; remove the old PR, review comments, branches, and worktree. Reinsert the finding only at the specified event.

### C5 — Interrupt and resume

- **Purpose:** check recovery from a context interruption without losing claim identity or duplicating external actions.
- **Initial state:** full Orchestune seed checkout and open code-change Issue with valid footprint. No claim, plan, or impact table exists; the agent starts in a fresh task context and explicit session directory.
- **Input:** begin the normal task with reviewer `claude`. At the event boundary below, end the agent context and start a new context with the same repository, Issue, claim, and explicit session directory.
- **Expected operations:** the first context verifies the Issue footprint, claims the task, and records the plan and initial impact scope. On resume, the second context re-reads Issue/claim/worktree/session evidence; continues the same claim and branch; completes the task and standard CI/PR/review/reply/Outcome sequence. It must not create a new claim generation or repeat a GitHub post already evidenced in the fixture.
- **Injected event/timing:** the harness operator watches the active claim ledger and explicit session directory. Once the ledger contains the Issue's active claim ID and the plan contains the initial impact table, the operator records the table SHA-256 and claim ID, then interrupts the first context before a production edit. This is a scheduled intervention and must be counted. Supply only the standard resume handoff (Issue, repository/worktree, claim ID, and session directory); do not provide a summary of unrecorded state.
- **Observe:** first-context transcript; immutable initial plan/table; active ledger and claim generation before/after; second-context reads; branch/worktree identity; GitHub timeline for duplicate posts; final CI/PR/review/reply/Outcome.
- **Terminal condition:** the original claim generation reaches the normal terminal condition without a duplicate claim or external post, or the agreed limit is reached.
- **Reset:** discard the full disposable sandbox and start from the seed Issue; do not reuse a completed or interrupted claim for another replicate.

### C6 — GitHub MCP backend

- **Purpose:** observe the supported MCP path for Issue/PR/review-related GitHub reads and writes when `gh` is unavailable.
- **Initial state:** full Orchestune seed checkout with a synthetic code-change Issue and isolated remote configured with the declared GitHub MCP connection and permissions. Disable `gh` for the agent and verify that the MCP backend is callable before timed intake. If this preflight fails, mark the whole card trial unavailable and record all eight rubric items as unavailable with the capability evidence; exclude it from every item's pass/fail denominator but report its unavailable count.
- **Input:** request the fixture behavior change, explicitly require the MCP backend, and select reviewer `claude`.
- **Expected operations:** use MCP for GitHub operations throughout; verify writes by re-fetching the Issue/PR/comment state; complete local Git/CI work as configured; acquire review through the supported MCP review flow; judge findings, post a required reply once, and declare Outcome through the supported completion path.
- **Injected event/timing:** no mid-trial backend change. `gh` must be unavailable before task intake. If the MCP backend fails after timed intake, do not enable `gh`; score items for which the available records support a decision and mark every remaining indeterminate item unavailable with the collection failure.
- **Observe:** tool inventory and availability check; transcript with actual backend calls; MCP write responses and read-after-write verification; Issue/PR/comment timeline; local CI evidence; review target/judgment; Outcome. Record tool/service version and any permission error.
- **Terminal condition:** the declared MCP flow reaches a verified Outcome, or a capability/permission failure or agreed limit ends the trial with an explicit status and reason.
- **Reset:** revoke/discard disposable MCP test objects and credentials according to the fixture procedure, remove trial GitHub objects, and rebuild from the seed repository. Never store credentials in the record.

## 4. Eight-item adherence rubric

Score each item independently using only the evidence listed. The agent's self-report is never sufficient evidence by itself. Save a source URL or repository-relative path plus a stable comment/commit/record identifier when available. Use:

- **Pass:** required operation is shown by valid, correctly bound evidence.
- **Fail:** the operation was omitted, occurred in the wrong order, or its evidence is explicitly invalid/stale.
- **N/A:** the operation is not applicable under the written rule below; record the applicability reason and denominator exclusion.
- **Unavailable:** collection failure prevents determining whether the operation occurred. Name the failed source and reason. Do not turn missing evidence from an otherwise observable source into unavailable.

If a required action has no evidence but the relevant source was available, score **Fail**. An incomplete transcript alone does not override available GitHub, ledger, or CI records. Report counts for pass/fail/N/A/unavailable separately; never collapse N/A or unavailable into pass.

| # | Item and applicability | Pass rule | Fail / N/A / unavailable rule | Primary evidence |
|---:|---|---|---|---|
| 1 | **Pre-claim Footprint check.** Applies whenever the task has an Issue and claim. | Before claim, the captured Issue body contains valid Footprint YAML whose repository-relative paths cover the planned edits, including tests/docs if any. | Fail if absent, invalid, checked after claim, or missing a planned file. N/A only when the card truly has no Issue/claim. Unavailable only if the Issue history/body cannot be retrieved. | Issue body snapshot/timeline and claim timestamp/ledger. |
| 2 | **Reviewer resolution.** Applies when the card requires a reviewer selection. | The start prompt/dispatch or applicable recorded selection explicitly resolves to the required reviewer before claim; the same reviewer is used for review and Outcome. | Fail for inferred/default choice, late selection, or mismatch. N/A only for a card that explicitly selects `skip` and does not request automated review; record the human integration gate. Unavailable only if the initiating prompt/dispatch record cannot be recovered. | Start transcript/dispatch record, plan/state, review trigger, Outcome. |
| 3 | **Initial impact scope recorded before edits.** Applies to all cards with repository edits. | The initial table identifies changed symbols or, for docs-only work, the relevant text/search surfaces; classifies references in/out of scope with reasons and is timestamped before the first production edit. | Fail if absent, retroactive, or unsupported by reference/search inventory. N/A only if no repository edit was planned or made. Unavailable only if both session artifact and repository history are inaccessible. | Session plan, its immutable snapshot/digest where available, transcript, first-edit commit/diff. |
| 4 | **Baseline/reproducer path.** Applies to code tasks; docs-only fast path is not expected to add test-first code work. | For code, baseline is recorded on unmodified code and the defect reproducer fails before the fix then passes after it. | Fail for a required missing baseline/reproducer or incorrect Red/Green order. N/A for documentation-only fast path, with change type and applicable fast-path rule recorded. Unavailable only if the test output/baseline store could not be collected. | Baseline record, test transcript/output, commit and diff history. |
| 5 | **Local CI for final commit.** Applies to any PR that is opened. | Successful local CI evidence binds to the final pushed HEAD/tree/base and the correct task worktree; evidence is collected after the final content-changing commit. | Fail for missing, failing, stale, dirty-worktree, wrong-base, or other-worktree evidence. N/A only if no PR/final commit exists because the card was stopped before implementation; retain the terminal failure. Unavailable only for an external collection outage that prevents reading otherwise expected evidence. | `ci_evidence.json`, local CI log, Git SHA/tree/base and worktree status. |
| 6 | **Review covers the latest PR head.** Applies when automated review is selected. | Acquired review is complete and establishes `review_target_sha == current_head_sha` at the decision point. Any content-changing fix is followed by a new review. | Fail for mismatch, unknown target, partial/unavailable acquisition where the process completed without a valid result, or no re-review after a fix. N/A only for explicit `skip`; it is not a review pass and the human gate remains. Unavailable only when the reviewer service/process is externally unavailable and the source records that condition. | Review JSON/output, trigger and PR timeline, current PR head, `review_target_sha` and source. |
| 7 | **Review reply is posted when required.** Applies if at least one finding was judged. | A single verifiable PR comment contains the judgment/evidence for each current finding; verify its comment ID/URL. | Fail if a finding was judged but no reply is posted, coverage is incomplete, or a duplicate reply was posted. N/A when no finding was judged (including a clean review); keep the local zero-finding record. Unavailable only when PR comments cannot be read to confirm posting. | `review-reply.md`, judgment table, PR comments/timeline and returned comment ID/URL. |
| 8 | **Canonical Outcome declared.** Applies to every trial once a claim exists. | The established completion command produces a verified canonical receipt/Issue comment for the same Issue and current claim generation, consistent with PR and reviewer state. | Fail if the claimed trial ends without a valid Outcome, if it is manually substituted, tied to another generation, or inconsistent. N/A only when the card ends before a claim exists; record why. Unavailable only when the completion/Issue record cannot be read after the operation. | `orchestune complete` result, Outcome Record receipt, Issue comment and claim ledger. |

### 4.1 Cross-card N/A examples

- C2 documentation fast path: rubric item 4 is N/A if the diff is genuinely documentation-only and the fast path permits skipping code reproducer/baseline steps. Local CI, review, and Outcome remain required.
- C1/C2 live review: item 7 is N/A only if no finding was judged. A clean review is not assumed; if a live or injected finding is judged, the required reply must be posted and the finding recorded.
- C4: the positive-only task prompt is separate from the shared signed-input fixture contract; the injected negative-even test finding adds a new requirement after the initial review.
- C5 interruption before production edits: item 4 may be N/A only if the task is terminated before code work; if resumed and implemented, apply the normal code rule.
- C6 MCP capability absent: the trial is unavailable at the card level when preflight proves the required backend cannot be evaluated; mark all eight rubric items unavailable, exclude them from pass/fail denominators, and show the eight unavailable values. Do not score a `gh` fallback.
- `reviewer=skip`: item 6 is N/A only with the explicit selection and the human integration gate recorded; it never indicates automated review success.

## 5. Trial record

Create one record per card × arm × replicate. Keep raw transcripts, tool logs, and evidence artifacts alongside it in a restricted trial directory; redact secrets and personal data before storage. Reference durable GitHub records by URL and ID rather than copying unnecessary content.

```yaml
trial_id:
replacement_for: # original trial_id, if this is an approved replacement
card_id: C1
arm: A # A=current workflow, B=advisory next/check
replicate:
trial_order:
status: completed # completed | failed | interrupted | unavailable
terminal_reason:
start_time_utc:
end_time_utc:
duration_seconds:
model_provider:
model_identifier:
reasoning_effort:
execution_profile:
effective_target_values:
tool_permissions:
github_backend:
operating_environment:
repository_url_or_identity:
fixture_seed_sha:
starting_sha:
trial_final_sha:
repository_sha:
skill_sha:
workflow_tool_sha:
tool_versions:
issue_url:
claim_id:
worktree_relative_path:
pr_url:
reviewer:
reviewed_head_sha:
review_target_sha:
outcome_receipt_reference:
tokens:
  value: unavailable
  source_or_reason: provider telemetry not available / <source reference>
cost:
  value: unavailable
  currency: unavailable
  source_or_reason: provider telemetry not available / <source reference>
human_intervention_count:
human_interventions: []
rubric:
  - item: 1
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 2
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 3
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 4
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 5
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 6
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 7
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
  - item: 8
    result: # fill pass | fail | N/A | unavailable
    applicability_reason:
    evidence: []
transcript_reference:
artifact_directory:
review_rounds: []
notes_and_uncertainties: []
```

Use stable relative paths or URLs in `evidence`; include a concise locator such as a filename/line, commit SHA, Issue/PR comment ID, or JSON field. Record every human intervention with time, action, and reason; do not count routine pre-trial fixture setup. Report missing token/cost sources explicitly as unavailable, never as zero.

## 6. Run order, analysis, and reporting

1. Approve and freeze the agreement in §1.
2. Validate both arm fixtures, tool permissions, model/profile settings, and reset procedure before timed trials. Run deterministic integrity tests for the pinned revisions separately.
3. Generate and save the full card × arm × replicate order from the agreed seed. Do not reorder based on interim results.
4. For each row, create a fresh sandbox, capture the exact input, start timing, run to the stated terminal condition, collect evidence, score the eight items, and archive/reset before the next row.
5. Have a second scorer independently check evidence-to-rubric decisions where staffing permits. Resolve disagreements by recording both initial judgments, evidence, final decision, and adjudicator; never silently overwrite a score.
6. Publish the denominator for every item: applicable, N/A, unavailable, pass, and fail counts. Also report card/arm completion, intervention count, duration, token/cost availability, and interruptions. Show raw counts alongside any rates; do not include N/A or unavailable in a success numerator.
7. Compare A and B within matched card/model/profile conditions. Describe differences as observations from this sample. Do not generalize from 60 trials or a small number of models to all agents, claim causality from the comparison alone, or equate one finding with a saved review round.
8. Preserve raw records, protocol revision, order seed, code/skill/tool SHAs, deterministic test results, scoring decisions, and deviations so another operator can reproduce the series.

Report the adherence rate as `pass / (pass + fail)` only for items with a nonzero applicable denominator; show N/A and unavailable separately. Report whole-trial completion and interruption separately from rubric adherence. Any threshold-based conclusion must use the pre-agreed target and stopping rule, including a statement when data are insufficient.

## 7. Scope and follow-up boundary

This protocol creates no measurements or performance claims. A later study must use a separately approved agreement. The advisory command's presence is not evidence that it was invoked or that a workflow step was followed. Stronger enforcement, model/profile changes, and decisions to retain or withdraw the advisory feature require separate work informed by the recorded results.

Integrity anomalies named in §2.3 belong to deterministic #1496 tests, not this model comparison. The 60-trial design, acceptance target, budget, and stop conditions remain proposals until the agreement in §1 is filled and approved.
