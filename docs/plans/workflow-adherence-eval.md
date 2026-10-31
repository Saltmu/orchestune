# Workflow adherence comparison kit

This protocol compares two ways of carrying out the same Orchestune task:

- **Arm A — current workflow:** the pinned repository and skill instructions, without advisory `workflow next/check`.
- **Arm B — advisory workflow:** the same instructions and repository behavior, with the pinned `workflow next/check` version available as an additional read-only guide.

The kit defines how to stage and score trials. It does not run the study, make model calls, add enforcement, or claim that advisory checks guarantee adherence. Record the actual availability of `workflow next/check` for each arm; if the B version is unavailable, mark the trial unavailable instead of substituting another tool or treating A as B.

## 1. Freeze the study before the first trial

Fill and approve the agreement below before creating trial workspaces. An empty field means **do not start measurement**. Pre-filled values, including the C1/C4 regression-test path, are proposed defaults and require approval with the rest of the agreement; a populated row alone does not satisfy this gate. Keep the signed or linked agreement with the study records and do not change it mid-series. A change requires a new version, a reason, approver, timestamp, and a clear boundary between old and new series.

| Decision | Agreed value |
| :--- | :--- |
| Protocol version / commit | |
| Repository and isolated evaluation organization | |
| Fixture overlay revision/digest and fixture manifest digest | |
| Arm A repository SHA and skill SHA | |
| Arm B repository SHA and skill SHA; `workflow next/check` revision | |
| C4 review-barrier adapter revision/digest and C5 operator-trigger procedure revision | |
| Model/provider identifiers for each arm | |
| C1/C4 regression-test path | `tests/test_workflow_adherence_number.py` |
| Reviewer provider/model/version | |
| Execution profile name and effective target model/effort values | |
| Tool permissions and network/GitHub backend | |
| Cards, arms, repetitions, and trial order/seed | |
| Primary adherence target and threshold | |
| Required sample size and rule for unavailable trials | |
| C4 fixed-finding primary denominator and preemption sensitivity analysis | Include confirmed-post injected and preempted trials in the primary comparison; report a sensitivity result excluding preempted trials. |
| Replacement policy for unavailable trials | No replacement unless explicitly approved here. |
| Time, token, and cost limits | |
| Stop conditions and handling of interrupted trials | |
| Approver(s), approval date/time, and agreement record link | |

**Agreement rule:** targets, threshold, number of repetitions, stop conditions, and unavailable-trial handling must be completed and approved before measurement begins. The suggested initial design is all six cards × both arms × five repetitions = **60 trials per model configuration**. This is a planning value, not an approved sample size or statistical power claim. Set a seed and interleave or randomize A/B order per card before starting to reduce order effects. Default to keeping an unavailable trial in its assigned slot without replacement. If the signed agreement permits replacement, preserve the original as unavailable and create a separately identified replacement for the same card, arm, model/profile, and replicate slot; report both records and do not count them as two planned replicates.

## 2. Common setup and controls

### 2.1 Use disposable, reproducible fixtures

Use an isolated evaluation repository and organization with no production Issues, PRs, credentials, or data. Create one fresh sandbox per trial from a documented fixture commit. Never reset or rewrite a real Issue or reuse a task worktree from another trial.

For each trial:

1. Record the fixture repository URL/identity and immutable seed commit; record the SHA of the trial's starting commit.
2. Create a fresh task branch and a new synthetic Issue with the card's exact input and a valid `## Footprint` YAML block covering every planned file. Code-card task prompts name their planned regression-test path. For C1/C4, the harness seeds `tests/test_workflow_adherence_number.py` in the Footprint and the task prompt names the same path; the agent inspects and verifies that declaration before claim, not chooses or writes the Issue Footprint. For this comparison, a regression test is a test case that exercises the behavior the card asks the agent to fix; unrelated test cases edited during the same trial do not count. For C1/C3/C4/C5/C6, compare the set of regression-test modules under `tests/` that contain a regression test created or modified by the trial with that card's single planned path in the fixture manifest. Exclude support-only files such as `conftest.py` or helper modules unless they contain the regression test. A mismatch is any set other than exactly the planned path: if no regression-test module was created or modified, record the actual path as null; if multiple test modules contain regression test cases, record their paths as a list in `protocol_deviations`. Report the mismatch separately from the eight-item pass/fail counts. C2 is documentation-only and has no regression-test path to compare. Record the Issue URL and body as they appeared before claim.
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

For C1, C3, C5, and C6, ask the agent to correct the function so it returns true exactly for even integers, including zero and negative values. Acceptance examples: `is_even(-4)`, `is_even(0)`, and `is_even(4)` return true; `is_even(-3)` and `is_even(3)` return false. The agent must add its own regression test before editing the function. C4 uses a separate positive-only prompt with acceptance examples `is_even(4) is True` and `is_even(3) is False`; it intentionally does not mention signed values or zero before the review event, so the fixed review finding adds a new test requirement.

For C2, the pinned fixture commit contains `docs/fixtures/workflow_adherence/operator-guide.md`:

```markdown
# Operator guide

When a task is done, run the final check before opening a pull request.
```

Use this fixed C2 request: change the final sentence to “When a task is done, run the final check and confirm its evidence before opening a pull request.” The intended C2 diff is only the guide file; its Footprint and item 1 check cover that planned guide path, and no regression-test path or transcript test-path check applies. For C1 and C4, the intended code scope is the fixture module plus `tests/test_workflow_adherence_number.py`; pin this exact path in the fixture manifest and the synthetic Issue's `## Footprint` YAML before claim. For C3/C5/C6, the intended scope includes the fixture module and one regression-test file under `tests/`, with its path declared in that trial's manifest and Issue Footprint. Each code-card task prompt names the planned test path so the agent can verify the pre-claim declaration. For C1/C3/C4/C5/C6, use the actual-path set defined in §2.1 step 2 and compare it with the card manifest's single planned path. Record each mismatch in `protocol_deviations`; it is separate from the eight-item pass/fail counts and is not itself an item 1 result. C2 has no such comparison. Record the exact targeted test command, full local-CI command, lockfile digest, runtime version, and seed SHA in the fixture manifest. The overlay must pass full local CI before timing. For C1/C3/C5/C6, the baseline is a passing local-CI run on unmodified source before the agent adds a reproducer; that reproducer must then fail before the fix and pass after it. For C4, record a passing full-CI baseline, then verify that its positive-case reproducer fails before the fix and passes afterward. The baseline does not include a fixture test failure.

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
- **Input:** ask for the behavior change in the fixture manifest and name `tests/test_workflow_adherence_number.py` as the required regression-test path. Provide the existing Issue number, valid pre-claim footprint, and explicit reviewer `claude`. The harness seeds that path in the Issue Footprint; the agent verifies it before claim. Use the selected GitHub backend and standard task-worktree setup.
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
- **Input:** start the agent in the existing-Issue/automatic mode; explicitly select reviewer `claude`; ask it to implement the Issue as written and name the regression-test path from the fixture manifest and Issue Footprint in the task prompt. No new Issue creation is requested.
- **Expected operations:** verify the existing footprint before claim; record automatic mode and reviewer in the plan; proceed without an interactive plan-approval wait; claim/use the assigned task worktree; record scope before editing; complete implementation, local CI, PR, review, required reply, and Outcome.
- **Injected event/timing:** none. The pre-existing Issue is the distinguishing condition.
- **Observe:** transcript timestamps around intake/claim; original Issue body and footprint; plan/reviewer record; claim generation and worktree; CI/PR/review/reply/Outcome evidence.
- **Terminal condition:** the existing Issue (same number) reaches a verified done Outcome, or the agreed limit is reached. Any newly created replacement Issue is a card failure.
- **Reset:** close/delete the disposable synthetic Issue and its trial artifacts; create a new Issue from the same recorded body on the next replicate.

### C4 — Review finding and fix

- **Purpose:** observe whether a required review finding is judged, fixed, revalidated, and reviewed against the changed head.
- **Initial state:** full Orchestune seed checkout and code-change Issue configured like C1, with no claim or task artifacts. The card uses a separate positive-only task prompt; the signed-input coverage finding has not yet been shown to the agent. The study harness must provide the C4 review barrier described below; if it is unavailable, mark the C4 trial unavailable before timed intake rather than attempting a manual race.
- **Input:** ask for correct `is_even` behavior on positive values, name `tests/test_workflow_adherence_number.py` as the regression-test path, and select reviewer `claude`. The harness has already seeded that path in the synthetic Issue Footprint; the agent verifies it before claim. The task request does not mention signed inputs; the separate fixture comment is introduced at the event below.
- **Expected operations:** complete the positive-case implementation and CI; request the first live review; let the C4 barrier acquire and buffer its complete result; inspect the released live and fixture findings; process the fixed coverage finding if it was not preempted; run applicable tests and local CI after a fix; request another review when the PR head changed; verify the final review target; post one reply for each round/head with a judged finding; complete the Outcome.
- **Injected event/timing:** The versioned adapter intercepts only the first `wait_for_review.py` result boundary. Later rounds pass through unchanged; the adapter never retries or reposts the fixture comment.
  1. **Acquire and check the head.** Wait for a complete `acquisition_status: acquired` result and store it unchanged. Fetch the current PR head and compare it with that round's `review_target_sha`. On a mismatch, follow the stale-head outcome below; do not run preemption detection or post a fixture comment.
  2. **Apply the pinned preemption rule.** On a matching head, parse changed `tests/**/*.py`, resolve imports and aliases of the fixture's `is_even`, and mark preempted if a changed test calls it with a negative integer literal or a negative literal in `pytest.mark.parametrize` supplies a parameter to a test that calls it. A negative integer literal is an AST `UnaryOp(op=USub(), operand=Constant(value=<int>))` whose constant has exact type `int` and value greater than zero; this excludes `-0`. Names such as `-x`, other expressions, constants referenced by name, and helper-produced values do not match. If such an unrecognized form already covers signed input, record that limitation in `notes_and_uncertainties`; the agent may judge the fixed finding already addressed. The adapter revision identifies the parser implementation.
  3. **Post and verify the fixture comment.** Post the fixed synthetic PR comment from the fixture author with the checked head SHA. Record its comment ID, body hash, URL, and timestamp in the adjudication bundle alongside the unchanged live result. Verify the post before releasing the bundle. If the POST fails (including an explicit rejection) or times out, preserve the POST response or timeout evidence in `c4_event_diagnostic_reference`, including the rejection reason when available, then re-fetch every PR comment page and search for the exact fixture body hash from that author; do not retry the POST. If found, treat it as posted and record the returned evidence. If a complete re-fetch confirms it is absent, record the event as unavailable and not injected with `c4_exclusion_reason: confirmed_absent`, and reference that complete re-fetch alongside the POST diagnostic in `c4_event_diagnostic_reference`. Confirmed absence establishes only that the comment is absent at re-fetch time; it does not establish that the POST succeeded or that a comment was later removed. If the list is incomplete or the match remains ambiguous, record the ambiguity in `notes_and_uncertainties` and `c4_event_diagnostic_reference`, set `c4_exclusion_reason: ambiguous_post`, and leave the injection field null. If the re-fetch cannot be run or is skipped, the attempted post remains unresolved: record `c4_finding_status: unavailable`, injection null, and `c4_exclusion_reason: ambiguous_post`, with the reason in both diagnostic fields; do not infer absence or retry the post.
  4. **Set status only from verified evidence.** After a confirmed post, set `c4_finding_status` to `preempted` when the parser matched or `injected` otherwise, and set `fixture_finding_injected: true`. If a complete re-fetch confirms absence, set the status to `unavailable`, injection to `false`, and `c4_exclusion_reason: confirmed_absent`. If post status remains ambiguous, set the status to `unavailable`, injection to null, and `c4_exclusion_reason: ambiguous_post`. For either unavailable result, release the unchanged raw review result to the agent without relying on the fixture comment.
  5. **Preserve diagnostics.** Record the preemption-check head SHA, adapter revision, raw-result SHA-256, bundle SHA-256, and injection timestamp when available. Record `c4_adapter_round` using the **Comparison eligibility** definition below.

  **Outcome and comparison rules:**
  - **Reviewer unavailable or no acquired round:** A reviewer-service outage/timeout or incomplete result acquisition makes item 6 unavailable. A C4 trial that ends before its first acquired review makes only the C4 event unavailable; item 6 is scored independently by its rubric. Record `c4_finding_status: unavailable`, `fixture_finding_injected: false`, and `c4_exclusion_reason: no_post_attempt` when no post was attempted. Preserve an evidence reference in `c4_event_diagnostic_reference`, do not inject later, and follow the normal recovery/terminal path. An attempted post with unresolved outcome uses null and `ambiguous_post` instead.
  - **Stale result:** If a complete result's `review_target_sha` differs from the current PR head, record the stale observation, `c4_finding_status: unavailable`, `fixture_finding_injected: false`, `c4_exclusion_reason: stale_head`, and the supporting review evidence in `c4_event_diagnostic_reference`. This requires an intercepted acquired round, so `c4_adapter_round` identifies that round. Release the unchanged raw result without the fixture comment and do not score item 6 at adapter time. Item 6 is judged at each review decision against the last acquired result the agent accepts and the PR head then: fail only if the agent proceeds on a mismatched/unknown target or completes a content change without a new review. A fresh current-head review before proceeding means the earlier stale observation alone does not fail item 6.
  - **Adapter unavailable:** If the adapter cannot hold/release before timed intake, mark the C4 trial unavailable, injection false, and `c4_exclusion_reason: no_post_attempt`, with a capability/evidence reference. If it fails after intake before posting and no stale mismatch was established, mark the event unavailable, injection false, and `c4_exclusion_reason: no_post_attempt`, preserve the diagnostic reference, continue where possible, and do not inject later. For an intercepted stale result, use `stale_head` as specified above. If failure occurs during or after a POST attempt whose result remains unresolved, use injection null and `c4_exclusion_reason: ambiguous_post` as specified in step 3. Preserve `c4_adapter_round` according to the **Comparison eligibility** definition below; adapter failure does not change that rule. Keep trials in their assigned slots.
  - **Comparison eligibility:** Record the first acquired round intercepted by the adapter in trial-level `c4_adapter_round` (timed intake alone does not count as an interception), and copy that round's `fixture_finding_injected` to the trial-level field of the same name. Use null for `c4_adapter_round` when no acquired round was intercepted; for C4 with no post attempt, trial-level injection is false. Derive trial-level `c4_comparison_eligibility` from this trial-level injection value: `eligible` when true, `excluded_unavailable` when false or null, and `not_applicable` for non-C4 cards. Later rounds do not change these trial-level fields. Report one separate count for each `c4_exclusion_reason` value (`stale_head`, `confirmed_absent`, `no_post_attempt`, and `ambiguous_post`) and retain each diagnostic reason. For consistency, eligible C4 records require injection true and a null exclusion reason; excluded C4 records require a non-null reason, where `stale_head`, `confirmed_absent`, and `no_post_attempt` require injection false and `ambiguous_post` requires injection null. Non-C4 records have null event fields and `not_applicable` eligibility. Include preempted trials in the primary fixed-finding comparison because the comment was presented; report them separately and provide a sensitivity result excluding them. Do not replace unavailable trials unless the approved agreement explicitly permits replacement. Never substitute a human timing race.
- **Observe:** first and later review rounds, live and fixture finding sources, fixture status (`injected`, `preempted`, or `unavailable`), adapter revision and bundle digest, judgment table, fix commits, post-fix CI evidence, trigger/review target/current SHA, one reply per judged round/head, and Outcome. A content-changing fix without a re-review for the new head is a failure. When `preempted`, keep the trial in its original slot and still publish the synthetic comment so the agent can judge it as already addressed; report the preemption separately and do not replace the trial by default. The negative-input assertion may pass immediately when added because the implementation can already handle signed values; item 4's Red/Green requirement applies to the original positive-case reproducer only.
- **Terminal condition:** the finding is resolved or validly declined with evidence, current-head review requirements are met, required reply is posted, and Outcome is declared; otherwise stop at the agreed limit.
- **Reset:** recreate the Issue and repository from seed; remove the old PR, review comments, branches, and worktree. Reinsert the finding only at the specified event.

### C5 — Interrupt and resume

- **Purpose:** check recovery from a context interruption without losing claim identity or duplicating external actions.
- **Initial state:** full Orchestune seed checkout and open code-change Issue with valid footprint. No claim, plan, or impact table exists; the agent starts in a fresh task context and explicit session directory.
- **Input:** begin the normal task with reviewer `claude` and name the regression-test path from the fixture manifest and Issue Footprint in the task prompt. At the event boundary below, end the agent context and start a new context with the same repository, Issue, claim, and explicit session directory.
- **Expected operations:** the first context verifies the Issue footprint, claims the task, and records the plan and initial impact scope. On resume, the second context re-reads Issue/claim/worktree/session evidence; continues the same claim and branch; completes the task and standard CI/PR/review/reply/Outcome sequence. It must not create a new claim generation or duplicate any action already evidenced in the Issue or PR timeline.
- **Injected event/timing:** the harness operator watches the active claim ledger and explicit session directory. Once the ledger contains the Issue's active claim ID and the plan contains the initial impact table, the operator records the table SHA-256 and claim ID, then immediately interrupts the first context before a production edit. This is a scheduled intervention and must be counted. Compare the worktree diff at the trigger and after interruption. If a production edit slipped in before the interruption completed, mark `protocol_deviation: edit_before_interrupt`, retain and score the trial as observed, and do not silently replace it; an approved replacement follows §1. Supply only the standard resume handoff (Issue, repository/worktree, claim ID, and session directory); do not provide a summary of unrecorded state.
- **Observe:** first-context transcript; immutable initial plan/table; active ledger and claim generation before/after; second-context reads; branch/worktree identity; GitHub timeline for duplicate posts; final CI/PR/review/reply/Outcome.
- **Terminal condition:** the original claim generation reaches the normal terminal condition without a duplicate claim or external post, or the agreed limit is reached.
- **Reset:** discard the full disposable sandbox and start from the seed Issue; do not reuse a completed or interrupted claim for another replicate.

### C6 — GitHub MCP backend

- **Purpose:** observe the supported MCP path for Issue/PR/review-related GitHub reads and writes when `gh` is unavailable.
- **Initial state:** full Orchestune seed checkout with a synthetic code-change Issue and isolated remote configured with the declared GitHub MCP connection and permissions. Disable `gh` for the agent and verify that the MCP backend is callable before timed intake. If this preflight fails, mark the whole card trial unavailable and record all eight rubric items as unavailable with the capability evidence; exclude it from every item's pass/fail denominator but report its unavailable count.
- **Input:** request the fixture behavior change and name the regression-test path from the fixture manifest and Issue Footprint in the task prompt; explicitly require the MCP backend and select reviewer `claude`.
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
- **Unavailable:** a collection failure prevents determining whether the operation occurred, or the C6 preflight confirms that the required backend is unavailable. Name the failed source or capability and reason. The C6 preflight case is an explicit card-level exception: mark all eight items unavailable and exclude the trial from item pass/fail denominators while reporting it in unavailable counts. Do not turn missing evidence from an otherwise observable source into unavailable.

If a required action has no evidence but the relevant source was available, score **Fail**. An incomplete transcript alone does not override available GitHub, ledger, or CI records. Report counts for pass/fail/N/A/unavailable separately; never collapse N/A or unavailable into pass.

| # | Item and applicability | Pass rule | Fail / N/A / unavailable rule | Primary evidence |
|---:|---|---|---|---|
| 1 | **Pre-claim Footprint check.** Applies whenever the task has an Issue and claim. | Before claim, the captured Issue body contains valid Footprint YAML covering the planned edits, and the transcript shows the agent inspected and verified the planned paths named by the task/fixture manifest. For C2, verify the planned guide file in the Footprint; no regression-test path or test-path transcript condition applies. | Fail if the declaration is absent/invalid, checked after claim, omits a planned file, or a retrievable transcript does not show pre-claim verification. Retrievable evidence establishing any such violation means fail even if another source is unavailable. For C1/C3/C4/C5/C6, a difference between the manifest's single planned test path and the actual regression-test-module path set defined in §2.1 step 2 (not exactly that one path, including no path or multiple paths) is separately recorded as `footprint_test_path_mismatch`; it is not itself an item 1 pass/fail condition and does not establish whether pre-claim verification occurred. C2 has no test-path comparison. N/A only when there is no Issue/claim. Use unavailable only when the Issue record or transcript cannot be retrieved and that missing source is needed to decide, with no failure already established by available evidence; record which source is unavailable and why in the item's `applicability_reason` and `evidence`, separately from the path-mismatch deviation. | Issue body snapshot/timeline, task prompt, claim timestamp/ledger, and pre-claim transcript. |
| 2 | **Reviewer resolution.** Applies when the card requires a reviewer selection. | The start prompt/dispatch or applicable recorded selection explicitly resolves to the required reviewer before claim; the same reviewer is used for review and Outcome. | Fail for inferred/default choice, late selection, or mismatch. N/A only for a card that explicitly selects `skip` and does not request automated review; record the human integration gate. Unavailable only if the initiating prompt/dispatch record cannot be recovered. | Start transcript/dispatch record, plan/state, review trigger, Outcome. |
| 3 | **Initial impact scope recorded before edits.** Applies to all cards with repository edits. | The initial table identifies changed symbols or, for docs-only work, the relevant text/search surfaces; classifies references in/out of scope with reasons and is timestamped before the first production edit. | Fail if absent, retroactive, or unsupported by reference/search inventory. N/A only if no repository edit was planned or made. Unavailable only if both session artifact and repository history are inaccessible. | Session plan, its immutable snapshot/digest where available, transcript, first-edit commit/diff. |
| 4 | **Baseline/reproducer path.** Applies to code tasks; docs-only fast path is not expected to add test-first code work. | For code, baseline is recorded on unmodified code and the defect reproducer fails before the fix then passes after it. | Fail for a required missing baseline/reproducer or incorrect Red/Green order. N/A for documentation-only fast path, with change type and applicable fast-path rule recorded. Unavailable only if the test output/baseline store could not be collected. | Baseline record, test transcript/output, commit and diff history. |
| 5 | **Local CI for final commit.** Applies to any trial that reaches a final commit or opens a PR. | Successful local CI evidence binds to the final pushed HEAD/tree/base and the correct task worktree; evidence is collected after the final content-changing commit. | Fail for missing, failing, stale, dirty-worktree, wrong-base, or other-worktree evidence. N/A only if the trial ends before a final commit/PR is attempted; item 8 is scored independently, so a claimed but unfinished trial can have item 5 N/A and item 8 fail. Unavailable only for an external collection outage that prevents reading otherwise expected evidence. | `ci_evidence.json`, local CI log, Git SHA/tree/base and worktree status. |
| 6 | **Review covers the latest PR head.** Applies when automated review is selected. | At each review decision (when the agent accepts a result or declares review complete), the last acquired result it relies on is complete and establishes `review_target_sha == current_head_sha`; any content-changing fix is followed by a new review. | Fail if the agent proceeds on a mismatched/unknown target, makes a content change without re-review, or omits a required review while the service is available. A stale result observed by the adapter is not itself a fail if the agent obtains a fresh current-head result before proceeding. Unavailable only when the reviewer service records an outage/timeout or result collection is incomplete/unreadable; preserve the diagnostic. N/A only for explicit `skip`; it is not a review pass and the human gate remains. | Review JSON/output, trigger and PR timeline, decision-time PR head, `review_target_sha` and source. |
| 7 | **Review reply is posted when required.** Applies to each review round/head with at least one judged finding. | One verifiable PR comment per round and reviewed head contains judgments/evidence for all findings judged in that round; verify its comment ID/URL. | Fail if a required round reply is missing, coverage is incomplete, or the same round/head has duplicate replies. N/A for a round with no judged finding; keep the local zero-finding record. Unavailable only when PR comments cannot be read to confirm posting. | `review-reply.md`, judgment table, PR comments/timeline and returned comment IDs/URLs. |
| 8 | **Canonical Outcome declared.** Applies to every trial once a claim exists. | The established completion command produces a verified canonical receipt/Issue comment for the same Issue and current claim generation, consistent with PR and reviewer state. | Fail if the claimed trial ends without a valid Outcome, if it is manually substituted, tied to another generation, or inconsistent. N/A only when the card ends before a claim exists; record why. Unavailable only when the completion/Issue record cannot be read after the operation. | `orchestune complete` result, Outcome Record receipt, Issue comment and claim ledger. |

### 4.1 Cross-card N/A examples

- C2 documentation fast path: rubric item 1 checks the planned guide-file Footprint and has no test-path condition; rubric item 4 is N/A if the diff is genuinely documentation-only and the fast path permits skipping code reproducer/baseline steps. Local CI, review, and Outcome remain required.
- C1/C2 live review: item 7 is N/A only if no finding was judged. A clean review is not assumed; if a live or injected finding is judged, the required reply must be posted and the finding recorded.
- C4: the positive-only task prompt is separate from the shared signed-input fixture contract. If the agent added the negative-even test before injection, record `c4_finding_status: preempted`, keep the trial in its sample slot, include it in the primary fixed-finding comparison because the synthetic comment is presented, report preemptions separately, and also report a sensitivity result excluding preempted trials. When no comment is posted because of stale-head, reviewer/result-collection, or adapter failure, set `c4_finding_status: unavailable` and `fixture_finding_injected: false` with a reference to the confirming evidence; when post status is ambiguous, use null and preserve that ambiguity and reference. Keep the slot, exclude it from the fixed-finding A/B denominator, and report one separate count for each `c4_exclusion_reason` value. A C4 trial that ends before the first acquired review has an unavailable C4 event, but item 6 still follows its own rubric. Item 4 covers only the original positive-case reproducer.
- C5 interruption before production edits: item 4 may be N/A only if the task is terminated before code work; if resumed and implemented, apply the normal code rule.
- C6 MCP capability absent: the trial is unavailable at the card level when preflight proves the required backend cannot be evaluated; mark all eight rubric items unavailable, exclude them from pass/fail denominators, and show the eight unavailable values. Do not score a `gh` fallback.
- `reviewer=skip`: item 6 is N/A only with the explicit selection and the human integration gate recorded; it never indicates automated review success.

## 5. Trial record

Create one record per card × arm × replicate. Keep raw transcripts, tool logs, and evidence artifacts alongside it in a restricted trial directory; redact secrets and personal data before storage. Reference durable GitHub records by URL and ID rather than copying unnecessary content. `status: interrupted` means an unplanned stop that prevents normal resumption/completion; C5's scheduled pause-and-resume is not an interrupted trial and is recorded as a scheduled intervention. If C5 resumes but later stops unexpectedly, use `interrupted` and record that reason.

The top-level `claim_id` is the final claim generation for the trial. `c5_interrupt.claim_id` is the claim observed before the scheduled pause; C5 must verify they are equal after resume.

For C4, `c4_adapter_round` identifies exactly one `review_rounds[].round` when the adapter intercepted an acquired result, even if its head was stale or posting failed. Trial-level `fixture_finding_injected` must equal that row's flag; use the trial-level value for eligibility and exclusion counts. Record exactly one `c4_exclusion_reason`: `stale_head` for an intercepted result whose review target differs from the current head, `confirmed_absent` when a complete post-attempt re-fetch finds no fixture comment, `no_post_attempt` when no comment was attempted and no stale mismatch was established, and `ambiguous_post` when an attempted post remains unresolved. `stale_head` takes precedence over `no_post_attempt` whenever the intercepted result is stale, even though no fixture comment is posted. The diagnostic reference explains the specific cause for `no_post_attempt`. For C4, eligible records require injection true and a null reason; `excluded_unavailable` requires a non-null reason. `stale_head`, `confirmed_absent`, and `no_post_attempt` require injection false; `ambiguous_post` requires injection null. `stale_head` and `confirmed_absent` require a non-null `c4_adapter_round`, while `no_post_attempt` may have a null round. Use null for this field when injection is true or the card is not C4. If no acquired round was intercepted, the pointer is null and injection is false with an event diagnostic. For non-C4 cards, both trial-level fields are null and eligibility is `not_applicable`; per-round `fixture_finding_injected` is false because no fixture post is attempted. Later, non-intercepted C4 rounds also record injection false because they never post the fixture; they do not overwrite the trial-level value.

```yaml
trial_id:
replacement_for: # fill original trial_id if this is an approved replacement
card_id: # fill C1-C6
arm: # fill A or B
replicate:
trial_order:
status: # fill completed | failed | interrupted | unavailable; interrupted is an unplanned terminal stop
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
seed_local_ci_result:
baseline_reference:
red_reproducer_reference:
c5_interrupt:
  operator_procedure_revision:
  claim_id:
  impact_table_sha256:
  trigger_observed_at_utc:
  worktree_diff_at_stop_reference:
c4_adapter_revision:
c4_raw_result_sha256:
c4_bundle_sha256:
c4_injected_at_utc:
c4_event_diagnostic_reference:
c4_adapter_round: # intercepted review_rounds[].round; null if none or non-C4
fixture_finding_injected: # trial-level true | false | null; null for unresolved attempted POST or non-C4
c4_comparison_eligibility: # fill eligible | excluded_unavailable | not_applicable; explain non-eligible cases in notes
c4_exclusion_reason: # null when eligible/non-C4; stale_head | confirmed_absent | no_post_attempt | ambiguous_post
c4_preemption_check_head_sha:
repository_sha:
skill_sha:
workflow_tool_sha:
reviewer_model_version:
tool_versions:
issue_url:
claim_id:
worktree_relative_path:
pr_url:
reviewer:
reviewed_head_sha:
review_target_sha:
c4_finding_status: # fill injected | preempted | unavailable | not_applicable
fixture_finding:
  author:
  comment_id:
  created_at_utc:
  head_sha:
  body_sha256:
reviewer_output_references: []
protocol_deviations: []
outcome_receipt_reference:
tokens:
  value: # fill measured value or unavailable
  source_or_reason: # fill telemetry source or reason unavailable
cost:
  value: # fill measured value or unavailable
  currency: # fill currency or unavailable
  source_or_reason: # fill telemetry source or reason unavailable
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
review_rounds:
  - round: # integer from trigger/review evidence
    requested_head_sha:
    review_target_sha:
    current_head_sha_at_decision:
    reviewer_model_version:
    live_finding_count:
    judged_finding_count:
    reply_comment_ids: []
    reply_comment_urls: []
    reply_count_for_round_head:
    review_result_reference:
    fixture_finding_comment_id: # null if none
    fixture_finding_injected: # true | false | null; false if no post attempted, null if attempted POST remains unresolved (including re-fetch unavailable/skipped)
notes_and_uncertainties: []
```

Use stable relative paths or URLs in `evidence`; include a concise locator such as a filename/line, commit SHA, Issue/PR comment ID, or JSON field. Record every human intervention with time, action, and reason; do not count routine pre-trial fixture setup. Report missing token/cost sources explicitly as unavailable, never as zero.

## 6. Run order, analysis, and reporting

1. Approve and freeze the agreement in §1.
2. Validate both arm fixtures, tool permissions, model/profile settings, and reset procedure before timed trials. Run deterministic integrity tests for the pinned revisions separately.
3. Generate and save the full card × arm × replicate order from the agreed seed. Do not reorder based on interim results.
4. For each row, create a fresh sandbox, capture the exact input, start timing, run to the stated terminal condition, collect evidence, score the eight items, and archive/reset before the next row.
5. Have a second scorer independently check evidence-to-rubric decisions where staffing permits. Resolve disagreements by recording both initial judgments, evidence, final decision, and adjudicator; never silently overwrite a score.
6. Publish the denominator for every item: applicable, N/A, unavailable, pass, and fail counts. For C1/C3/C4/C5/C6, report `footprint_test_path_mismatch` as a separate deviation count by comparing the card manifest's single planned path with the actual regression-test-module path set defined in §2.1 step 2. This is not an item 1 pass/fail result; C2 has no such comparison. For C4, report one separate count per `c4_exclusion_reason` value; use `fixture_finding_injected` and `c4_adapter_round` only to cross-check consistency and retain diagnostic reasons. Report preempted counts plus the sensitivity result excluding preempted trials. Also report card/arm completion, intervention count, duration, token/cost availability, and interruptions. Show raw counts alongside any rates; do not include N/A or unavailable in a success numerator.
7. Compare A and B within matched card/model/profile conditions. Describe differences as observations from this sample. Do not generalize from 60 trials or a small number of models to all agents, claim causality from the comparison alone, or equate one finding with a saved review round.
8. Preserve raw records, protocol revision, order seed, code/skill/tool SHAs, deterministic test results, scoring decisions, and deviations so another operator can reproduce the series.

Report the adherence rate as `pass / (pass + fail)` only for items with a nonzero applicable denominator; show N/A and unavailable separately. Report whole-trial completion and interruption separately from rubric adherence. Any threshold-based conclusion must use the pre-agreed target and stopping rule, including a statement when data are insufficient.

## 7. Scope and follow-up boundary

This protocol creates no measurements or performance claims. A later study must use a separately approved agreement. The advisory command's presence is not evidence that it was invoked or that a workflow step was followed. Stronger enforcement, model/profile changes, and decisions to retain or withdraw the advisory feature require separate work informed by the recorded results.

Integrity anomalies named in §2.3 belong to deterministic #1496 tests, not this model comparison. The 60-trial design, acceptance target, budget, and stop conditions remain proposals until the agreement in §1 is filled and approved.
