# Local CI workflow guidance and evidence gates

This document turns the approved design in [Issue #1417](https://github.com/Saltmu/orchestune/issues/1417) into an implementation contract for the follow-up issues. The first release is advisory and read-only. It derives a workflow stage from existing evidence; it does not add a second lifecycle state machine or weaken existing claim, CI, review, or completion requirements.

The detailed comparison-study protocol belongs in [`workflow-adherence-eval.md`](workflow-adherence-eval.md), the sibling deliverable tracked by [Issue #1493](https://github.com/Saltmu/orchestune/issues/1493). Until that sibling document is integrated, this relative link is expected to be unresolved on this branch. This document gives only the evaluation summary and its handoff.

### Merge prerequisite

Integrate the PR for #1493 into `parent/issue-1417` before integrating this PR, or integrate both together. This makes the sibling evaluation document available when the relative link is checked on the parent branch. The dependency and order are also recorded in [Issue #1493](https://github.com/Saltmu/orchestune/issues/1493#issuecomment-6545421448).

## Goals and boundaries

- Make the next workflow action and its required evidence explicit.
- Keep claim generation, worktree ownership, CI evidence, PR state, review rounds, and completion receipts as the sources of truth they already are.
- Store only facts that cannot be recovered from those sources: execution mode, change kind, GitHub backend, reviewer selection, plan-approval record, and the initial impact-table digest.
- Keep `workflow next` and `workflow check` read-only. They do not claim tasks, run CI, fetch Git, post comments, update labels, or complete work.
- Begin with warnings and instructions. Do not add new `complete` gates or server-side enforcement in this rollout.

Out of scope: implementing the CLI or stage loader in this document task, deciding that advisory checks guarantee compliance, judging whether an impact classification is substantively correct, or choosing stronger enforcement before evaluation.

## Current workflow and validation inventory

The mapping below is a design baseline for the follow-up implementation. “Implemented” means an existing mechanical check can be reused. “Missing” means the fact is currently recorded only in instructions or user-authored artifacts. “Decision needed” identifies a policy that remains advisory or belongs to a later evaluation.

| Workflow item | Current behavior | Status for workflow guidance |
|---|---|---|
| Claim generation and ownership | Claim/complete contracts, active ledger records, and worktree ownership are maintained by existing commands. | Implemented source of truth; derive, do not duplicate. |
| Clean worktree and final completion | `orchestune complete --result done` checks the current claim generation and task worktree and rejects a dirty worktree. | Implemented; expose the result and evidence. |
| CI freshness | CI evidence binds the HEAD, tree, base, CI definition and lockfile digests, environment, and clean worktree. The local CI script invalidates prior evidence before running. | Implemented in `orchestune/complete/ci_evidence.py`; reuse its read-only validator. |
| Review freshness and findings | Completion validates judgment-table source coverage and that `review_target_sha` equals the current HEAD. | Implemented in `orchestune/complete/review_evidence.py`; reuse the validator and current PR evidence. |
| Review acquisition | `scripts/wait_for_review.py` posts/validates triggers and acquires review content; exit 0 means acquired, not approved. | Implemented procedure; next/check only report its evidence and point to the procedure. |
| Plan approval and reviewer selection | Mode and selection rules exist in instructions, but approval/reviewer selection are not persisted on the claim. Reviewer is supplied to completion. | Missing durable record; store in workflow state and plan marker. |
| Initial impact-scope table | Instructions require an enumerated, classified table and preserve the initial version, but no central mechanical validator confirms its existence or rationale quality. | Structure may be checked; semantic correctness remains human/model judgment. |
| Red/baseline evidence and required PR sections | Instructions and templates describe them, but the current completion contract does not require their contents. | Missing as enforced gates; guidance may report missing evidence. |
| Pre-claim footprint | Claim validates its own footprint/ownership conditions. The interactive reviewer-choice contract is not part of a standalone workflow check. | Reuse narrow read-only footprint validation; do not call the claim service. |
| Enforcement against direct operations | GitHub CLI, Git operations, and MCP writes can bypass local guidance. The existing server workflow runs local CI for PR heads; no server workflow enforces plan approval or impact classification. | Decision boundary; see [Enforcement boundary](#enforcement-boundary). |

The read-only check must not call a command merely because it has a `--no-apply` option. For example, the current claim path acquires a run-state lock even in validation mode. If an existing rule is only available behind a side-effecting service, add a narrow reader/adapter around the established rule or report that the check cannot verify it. A read-only workflow layer must not import the claim/complete service layer to gain access to that rule.

## Vocabulary

### Derived stages

Stages are display values inferred from current evidence. They are not persisted and are not a one-to-one projection of `ClaimStage` or `CompleteStage`; those enums continue to describe progress inside claim and complete operations.

| `stage_id` | Entry condition and derived evidence | Suggested next action |
|---|---|---|
| `prepare` | Mode, backend, change kind, applicable reviewer choice, and plan/footprint context are not yet recorded. | Confirm the path and write the plan and initial footprint. |
| `claim` | Preparation exists, pre-claim is ready, and there is no matching active claim. | Run the established claim operation. |
| `scope` | A matching claim and task worktree exist; the initial impact table is absent or unrecorded. | Enumerate references and record the initial table before editing. |
| `implement` | Impact table is recorded and no current-head local CI evidence exists. | Follow the applicable Red/Green or documentation fast path, implement, commit, then run local CI. |
| `pr` | Pre-PR evidence is ready and there is no target PR. | Create the PR with the resolved base and completed template. |
| `review` | A target PR exists, but current-head review acquisition, judgments, or required reply evidence is missing or stale. | Use the existing bounded review procedure. |
| `complete` | Pre-complete evidence is ready for the active claim generation and current PR. | Run the established `orchestune complete` command. |
| `blocked` | A canonical blocked Outcome exists for the current claim generation. | Show the recorded reason and recovery reference; do not automatically restart. |
| `handed-off` | A canonical `handed_off` receipt exists for the current claim. | Report completion and point to the existing GC handoff. |

Evaluate terminal evidence first, then determine the earliest unmet stage in workflow order. Exactly one of a current-generation `handed_off` receipt or canonical `blocked` Outcome may determine the terminal stage. If both are present for the same generation, report stage `blocked`, aggregate `unmet` with exit 2 and reason `terminal_receipt_conflict`; this is contradictory evidence, not a verified `terminal_blocked` result. Report the shared claim ID when both receipts identify the same current claim; if their claim IDs disagree or cannot be established, return `claim_id: null` and include the conflict evidence. Do not prefer one receipt. A stage is not advanced by a user checkbox or a stale artifact. If required evidence cannot be read or is inconsistent, keep the earliest affected stage and report `unmet` with a reason code. `reviewer=skip` is a recorded choice, but it is not a successful review: the existing human integration gate still applies.

For `next`, `stage_id` is the derived current stage. For `check`, `stage_id` is also the derived current stage, while `gate_id` names the specifically requested gate; they answer different questions. A check of a later gate still reports an earlier unmet stage and returns aggregate `unmet` (exit 2), even if the later gate's own checks pass. This prevents a gate from being treated as permission to skip an earlier stage. For any `check` request in a verified blocked terminal stage, return `terminal_blocked` (exit 4), regardless of which gate was named. For any `check` request in a verified handed-off terminal stage, return `ready` with reason `handed_off` and no actions, regardless of which gate was named. If terminal evidence conflicts, `terminal_receipt_conflict` takes precedence: `next` and every `check` gate return `unmet` (exit 2), not `terminal_blocked`.

The `next` and `check` commands return the aggregate `status` and `reason_codes` defined in [Gates and checks](#gates-and-checks); `stage_id` identifies the derived stage and is never a verdict. In particular, `stage_id: blocked` with `reason_codes` containing `terminal_receipt_conflict` is `unmet`, not a verified terminal block. An `unmet` or `waiting_human` result may include diagnostic or decision actions, but callers must follow its status and reason codes rather than treating those actions as authorization to proceed.

For a gate earlier than the derived stage, re-evaluate that gate against current evidence; do not assume it is ready because the workflow advanced past it. For example, `pre-claim` uses the current Issue footprint and the current generation's recorded mode/reviewer/approval, while `pre-pr` uses current CI evidence and the existing PR's current head, base, and description. Return `ready` only if those bindings still validate; report stale or missing evidence as `unmet`.

`next` reports a verified `handed-off` terminal stage as `ready` with no suggested actions and a `handed_off` reason code; exit 0 means that the workflow has reached its successful terminal handoff, not that another action remains. A verified blocked terminal stage is `terminal_blocked` with exit 4; place its recorded reason in `actions` and its recovery procedure in `references`. In both cases the terminal receipt is included in `checks` and `artifacts`; its check uses the stable condition ID `terminal_receipt`, declared by the terminal stage in `stages.toml`.

### Gates and checks

`gate_id` identifies the contract being checked, independent of the current stage. The four gate IDs are stable machine-facing strings.

| `gate_id` | Required checks | Reused rule or evidence | Structural limit |
|---|---|---|---|
| `pre-claim` | Existing Issue footprint is valid and covers the planned files; required execution mode is known; interactive mode has a recorded reviewer choice from `claude`, `codex`, `skip`; required plan approval is recorded as approved. | Narrow footprint parser/validator and workflow state. | Does not acquire a claim or change labels. Auto mode follows its resolved reviewer policy. In interactive mode, a missing reviewer choice or required `plan_approval.status: pending` returns `waiting_human`; an invalid reviewer or malformed approval value returns `unmet`. |
| `pre-edit` | Current claim matches Issue and task worktree; initial impact table is present and has a reference, decision, and rationale for each row. | Active claim/ownership snapshot and parsed plan artifact. | Parser cannot certify that search results are complete or classifications are correct. |
| `pre-pr` | Local CI evidence is current for HEAD/tree/base and worktree is clean; expected PR description sections are present; the target branch follows the parent/stack rule. | Read-only CI evidence validator, GitHub PR snapshot, PR template contract. | Does not run CI, create a PR, or prove that CI occurred before an out-of-band PR was opened. |
| `pre-complete` | Existing completion preconditions, complete current-round finding judgments, current `review_target_sha`, required PR reply, and current claim generation are satisfied. | Existing read-only complete validators and GitHub review/PR snapshots. | Does not post a reply, trigger review, or create an Outcome. |

Checks return `ready`, `unmet`, or `waiting_human`; each check names the evidence it read and any pending work, missing evidence, or stale condition. For `next`, a check is `ready` when its evidence is valid and sufficient to identify a safe next action, even if the associated exit condition is ordinary work that remains to be done; that work appears in `missing`. Any invalid, stale, unreadable, contradictory, or unknown evidence in an emitted check is `unmet` with a reason code, whether or not another check would still suggest an action. A required human decision is `waiting_human`. For `check`, status reports whether the gate condition is satisfied; unsatisfied gate conditions have no pending-work exemption and are `unmet`. A non-mechanical judgment is never reported as mechanically ready merely because a table has the right shape. Aggregate precedence is `unmet` over `waiting_human` over `ready`: for example, a missing reviewer choice together with stale CI returns `unmet` (exit 2), because the stale CI is invalid evidence even though a human choice is also pending.

For `next`, emit one check for each condition ID in the current stage's `exit_conditions` array in `stages.toml`. Each emitted `checks[*].id` is that condition ID; the current stage's `gate` identifies the gate contract those exit conditions support and does not replace or rename their IDs. The `gate` field may be empty as specified in the stage schema; in that case the checks still describe the stage's exit conditions without a gate association. For `check <gate>`, emit every `exit_conditions` entry from the single stage whose `gate` equals the requested gate, plus every `exit_conditions` entry from earlier applicable stages. Earlier applicable stages are non-terminal stages with lower `order` whose branch predicates match the recorded mode, change kind, and backend. Each emitted `checks[*].id` is its declared condition ID, not a gate ID. Aggregate all emitted statuses using the precedence above.

## Evidence lifetime

| Evidence | Bound to | Becomes stale when |
|---|---|---|
| Claim and worktree ownership | Repository, Issue, claim ID/generation, branch and worktree | Claim generation or ownership changes, or the observed worktree is not the claimed worktree. |
| Initial impact table | Claim generation and recorded initial digest | Claim generation changes. Later discoveries are appended separately; they do not rewrite the initial snapshot. |
| Plan approval and reviewer choice | Claim generation, selected mode and plan identity | Claim generation changes or the plan conflicts with the state record. Reviewer changes require the existing explicit reviewer-switch procedure. |
| CI result | HEAD, tree, base, CI definition and lockfile digests, environment, and clean worktree | Commit/amend/rebase changes a binding; uncommitted changes appear; CI definition/lockfile changes; or evidence is from another worktree. |
| PR description and base | PR number, current base and PR head | PR head/base changes or required content is removed. |
| Review judgments | PR, current review round and its source items, `review_target_sha` | A new finding source appears, round changes, target SHA differs from current HEAD, or required reply is absent. |
| Completion receipt | Issue and current claim generation | A new claim generation is started; receipts from older generations remain historical. |

Unknown, unreadable, partial, stale, or contradictory evidence cannot satisfy a check. Preserve the actual `reviewed_head_sha` meaning from review acquisition; do not substitute a requested SHA or current local SHA. A review is current only if the evidence establishes `review_target_sha == current_head_sha`.

## CLI contract

Names are the planned public interface for the follow-up implementation:

```text
orchestune workflow next --issue N --session-dir PATH [--json]
orchestune workflow check <pre-claim|pre-edit|pre-pr|pre-complete> --issue N --session-dir PATH [--pr N] [--pr-body PATH] [--review-reply PATH] [--json]
```

The session directory is explicit. Do not choose the most recently modified directory or infer a session from unrelated files. `check` validates the requested gate only; `next` reports the earliest stage whose exit conditions remain and its next actions. Neither command creates or updates `workflow-state.json`. An actor performing the documented action updates the state and artifacts through the normal workflow.

### JSON result

Both commands emit one UTF-8 JSON object when `--json` is set. Schema version 1 has this shape; nullable values are explicit `null`, and unknown evidence remains unknown.

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | integer | `1` for this contract. |
| `issue_number` | integer | Requested Issue. |
| `claim_id` | string or null | Current claim generation ID; for handed-off, use the verified receipt's claim ID. Null before claim, when no current claim is evidenced, or when conflicting terminal receipts do not agree on the generation. |
| `stage_id` | enum string | One of the derived stage IDs above. |
| `gate_id` | enum string or null | Requested gate for `check`; always `null` for `next`. |
| `status` | enum string | `ready`, `unmet`, `waiting_human`, or `terminal_blocked`. |
| `reason_codes` | array of strings | Stable machine-readable causes; empty only when no reason applies. |
| `checks` | array | Objects with `id`, `status`, `evidence`, and `missing`; each `evidence` entry identifies source and observed value or SHA. For `next`, `id` is a condition ID from the current stage's `exit_conditions` in `stages.toml`. For `check`, it is a condition ID from each included stage's `exit_conditions`; IDs are not replaced by the stage's `gate`. |
| `actions` | array | Suggested argv arrays or user-facing action descriptions; never auto-executed. |
| `references` | array | Relative repository paths or canonical URLs for relevant instructions and evidence. |
| `artifacts` | object | Explicit paths and digests for plan, state, initial scope table, PR body, and review reply when present. |
| `observed_head_sha` | string or null | HEAD read from the verified task worktree; null if it could not be established or the handed-off worktree is no longer available. Do not substitute a historical receipt SHA for the observed current HEAD. |

`checks[*].status` uses `ready`, `unmet`, `waiting_human`, or `unknown`. `evidence` may be empty; `missing` explains absent, stale, unreadable, or contradictory inputs. Never turn `unknown` into `ready`: any required `unknown` check makes the aggregate result `unmet` with exit 2 and an unavailable reason code. Aggregate `waiting_human` applies only when the unresolved condition specifically requires a human decision and no contradictory invalid evidence exists.

Example of ready evidence:

```json
{
  "schema_version": 1,
  "issue_number": 1494,
  "claim_id": "claim-123",
  "stage_id": "pr",
  "gate_id": "pre-pr",
  "status": "ready",
  "reason_codes": [],
  "checks": [
    {"id": "ci_current", "status": "ready", "evidence": [{"source": "ci_evidence.json", "head_sha": "<40-hex-sha>", "tree_sha": "<tree-sha>"}], "missing": []},
    {"id": "worktree_clean", "status": "ready", "evidence": [{"source": "git status", "clean": true}], "missing": []}
  ],
  "actions": [{"description": "Create the PR using the resolved base branch."}],
  "references": ["skills/local-ci-developer/references/pr.md"],
  "artifacts": {"plan": {"path": "implementation-plan.md"}},
  "observed_head_sha": "<40-hex-sha>"
}
```

### Exit codes

| Code | Status | Meaning |
|---:|---|---|
| `0` | `ready` | Requested gate is satisfied, or `next` can derive a stage and safe next action even when ordinary exit conditions remain unmet. |
| `2` | `unmet` | Required evidence is absent, stale, unreadable, contradictory, or mechanically invalid. `reason_codes` explains why. |
| `3` | `waiting_human` | A required human approval or selection is pending. Keep the claim/worktree; this is not terminal. |
| `4` | `terminal_blocked` | A canonical blocked Outcome for the current claim generation has been verified. This reports an existing terminal state and never declares one. |

For `next`, return `waiting_human` (exit 3) when a required human approval or selection is pending, including at `prepare`; include the requested decision in `actions`. Otherwise return `ready` (exit 0) whenever evidence is sufficient to derive a stage and provide a safe next action; ordinary exit conditions still to be completed appear in `checks[*].missing` and do not prevent this result. Return `unmet` (exit 2) when evidence needed to derive the stage or validate a safe action is invalid, stale, unreadable, unknown, or contradictory. A successful terminal `handed-off` result is also `ready` with no actions. `check` evaluates its named gate and returns `unmet` when that gate or an earlier required stage is unmet.

Skills must pass `--json` whenever they branch on workflow status. In JSON mode, a usage/argument error is identified by the absence of a workflow-result object on stdout; stderr diagnostics are independent and do not change that test. A valid workflow-result JSON with exit 2 means `unmet`. Without `--json`, command errors are distinguished by the CLI's error stream and do not produce the workflow-result object. Unexpected internal errors return generic error exit 1 and no workflow-result JSON. Invalid user workflow state, including malformed state TOML, is validly read workflow evidence and returns `unmet` (exit 2) with a reason code. A missing or malformed packaged `orchestune/workflow/stages.toml` is a tool failure: return generic error exit 1 with diagnostics on stderr and no workflow-result JSON. In JSON mode, a successfully produced result uses the exit code associated with `status`.

## Persisted workflow context and plan marker

Store only non-derivable decisions in `<session-dir>/workflow-state.json`. This file is session-scoped context, not a competing lifecycle ledger.

| Field | Type / allowed values | Rule |
|---|---|---|
| `schema_version` | integer, `1` | Reject unsupported versions with `unmet`. |
| `repository` | stable repository identity | Must match the current worktree repository. |
| `issue_number` | integer | Must match the requested Issue. |
| `claim_id` | string or null | Null pre-claim; bind to the acquired generation after claim. Reject a different active generation until explicitly reconciled. |
| `mode` | `interactive` / `auto` | Record how execution was started. |
| `change_kind` | `code` / `docs` | Selects the applicable workflow path. |
| `backend` | `gh` / `mcp` | Fixed for GitHub writes in this task unless an authorized transition is recorded. |
| `reviewer` | `claude` / `codex` / `skip` | Record the selected reviewer; `skip` never means review passed. |
| `selection_basis` | string | Record prompt/dispatch/user selection provenance. |
| `plan_approval` | `{required: boolean, status: pending|approved|not_required, evidence: string|null}` | Record approval applicability and evidence; approval-required interactive work cannot be treated as approved by an empty field. |
| `impact_initial` | `{relative_path: string, digest: string}` or null | Digest the immutable initial table; append implementation discoveries elsewhere. |

The workflow executor (the human or agent carrying out the task, including an auto-dispatched agent) writes the initial context file and plan marker during preparation, before claim, and updates `claim_id` after the established claim command succeeds. `next` and `check` remain readers and never write these records. A dispatcher may supply initial execution metadata, but it does not own later plan approval or impact-table judgments.

Compute the initial impact digest over the exact Markdown table text only, normalized to UTF-8 with LF line endings and one final newline, before any reconciliation is appended. This digest is not a JSON encoding. The state JSON itself uses UTF-8 and deterministic key ordering for reproducible serialization, and stores no absolute paths.

Add one YAML block to the plan so tools can locate the state record without searching:

````markdown
```orchestune-workflow
schema_version: 1
state: workflow-state.json
```
````

Parse the fenced block as YAML with a safe loader, allow only the documented scalar keys, and require exactly one `orchestune-workflow` marker in a plan. Zero markers means the state locator is absent; multiple markers or unsupported keys are ambiguous and return `unmet`. The `state` path is relative to the plan file. The workflow marker is a locator and schema declaration, not evidence that approval or any gate passed. If the plan marker, state, Issue, or active claim disagree, return `unmet` with a conflict reason and show how to reconcile it; do not choose whichever value is more convenient. Reject absolute paths and paths that escape the session directory.

## Stage definitions file

The machine-readable source of stage metadata is `orchestune/workflow/stages.toml`, included as a package resource in built wheels. It describes guidance and branches; Python code owns executable validators. Never evaluate TOML values as code.

Top-level fields:

| Field | Type | Contract |
|---|---|---|
| `schema_version` | integer, `1` | Required. Unknown versions fail closed. |
| `stage` | array of tables | Ordered stage definitions with unique IDs. |

Each `[[stage]]` has:

| Field | Type | Contract |
|---|---|---|
| `id` | string | Stable stage ID from the vocabulary table. |
| `order` | positive integer | Unique order for ordinary progression. Terminal stages are evaluated before ordinary stages. |
| `gate` | known gate ID or empty string | Gate associated with entering/exiting this stage. |
| `entry_conditions` | array of condition IDs | References to allowlisted validator IDs, not expressions. |
| `actions` | array of action tables | Each action provides a description, optional argv template, and references; commands are displayed, never run. |
| `exit_conditions` | array of condition IDs | Allowlisted evidence checks for leaving the stage. |
| `on_failure` | array of action tables | Recovery guidance with a reason-code mapping. |
| `references` | array of relative paths/URLs | Existing procedure documentation. |
| `artifacts` | array of artifact IDs | Expected artifact names and formats. |
| `branches` | table | Enumerated `mode`, `change_kind`, and `backend` choices; no dynamic evaluation. |

The loader validates field types, unique stage IDs/orders, known gate/condition/action IDs, relative resource paths, and enum values. Missing or malformed package data is an error, not an empty permissive workflow. Existing detailed procedures remain authoritative for how to perform actions; TOML is authoritative for stage metadata and the mapping from those actions to stages.

Terminal stages (`blocked` and `handed-off`) declare the condition ID `terminal_receipt` in `exit_conditions`; `checks[*].id` uses this stable ID for their terminal receipt check.

Each non-empty stage `gate` value must be unique across `stages.toml`, so a requested gate identifies exactly one stage. The stage's full `exit_conditions` array is the complete list of IDs contributed by that stage to that gate; do not invent a gate-scoped ID or omit a gate check from the array. `check <gate>` emits every condition in this list, not a subset, as well as the conditions from earlier applicable stages.

## Lifecycle, interruption, and retry

1. **Prepare.** Resolve interactive/auto mode, code/docs change kind, GitHub backend, reviewer policy, plan approval applicability, and initial Footprint before claim. Existing Issue Footprint is checked before claim. For an interactive reviewer choice, accepted values are `claude`, `codex`, and `skip`; a prompt that names the reviewer is the selection evidence.
2. **Claim.** The user or dispatcher runs the established claim command. The workflow reader observes the active claim and its worktree; it does not call claim with `--no-apply` as a substitute for a reader.
3. **Scope.** In the claimed worktree, record a first-version impact table before production edits. Preserve it and its digest. After implementation, append `done`, `still out of scope`, or `unverified` reconciliation without rewriting the original decisions.
4. **Implement and verify.** Apply the code or documentation path, collect the applicable baseline/reproducer evidence, and run local CI. CI evidence is valid only for all of its bound values and a clean task worktree.
5. **Open PR.** Resolve the base using the existing stacked-dependency/parent-mode rules, prepare the required sections, and create the PR through the selected backend.
6. **Review.** Use the bounded review acquisition process. Acquisition is not a verdict. Judge every distinct current finding, post the required review reply, and re-review when a fix changes HEAD.
7. **Complete.** Run the existing complete validator/command for the current claim generation and current PR evidence. The workflow layer does not write the Outcome.

| Situation | Required behavior |
|---|---|
| Approval/reviewer choice pending | `waiting_human` (exit 3); before claim, do not start the claim; after claim, retain the active claim/worktree. Resume after the selection is recorded. |
| Interruption or context compaction | Re-read the explicit session state, active ledger/Issue, worktree, PR, and evidence. Derive the stage again; never trust a previously printed stage as current. |
| Resume in another worktree | Require repository, Issue, claim ID, branch, and worktree ownership to match. Evidence from a different task worktree is stale. |
| HEAD changed after CI | Invalidate the CI check and direct the user to run local CI for the new HEAD. |
| HEAD changed after review | Invalidate review readiness and require a fresh round for the new HEAD. |
| Transient read failure | Return `unmet` with a specific unavailable reason; a later read may retry. Do not post or mutate as a retry side effect. |
| Existing canonical blocked Outcome | Return `terminal_blocked` (exit 4), show its reason and established recovery instructions. Do not create another blocked Outcome. |
| Claim generation changed | Reconcile state with the current generation; prior-generation selections and initial-scope digest cannot silently satisfy the new generation. |
| Reviewer is `skip` | Continue to the established human integration gate; never report an automated review pass. |

## Read-only architecture and integration

The `orchestune.workflow` package holds L2 domain logic and read-only checks; `orchestune.workflow.cli` is an L4 entrypoint. There is no L3 workflow service in the first advisory implementation. Follow the [module-layer and package-boundary rules](../en/architecture.md#4-module-layers--package-boundary). Workflow L2 modules may import L0/L1 modules and other L2 validation modules such as `claim.*` and `complete.*` (including pure/read-only CI and review evidence validators), subject to §4.2 rule 5 cross-package cycle checks. All Git and GitHub reads go through the L1 adapters (`infra.git_cli` and `forge.*`); workflow modules must not invoke `git` or `gh` directly. The implementation issue must add every new `workflow.*` module to the layer table in both `docs/en/architecture.md` and `docs/ja/architecture.md`, including `workflow.cli` in L4 and domain modules in L2. Workflow L2 modules must not import L3 `claim.service` or `complete.service`, and no L2 domain module may import the L4 `workflow.cli` entrypoint. They may read the active claim ledger, task worktree/Git status, Issue/PR/review snapshots through the selected backend, plan/state files, and CI evidence. They may call pure/read-only validation functions. They must not acquire `run_state_lock`, run CI, fetch Git, post GitHub comments, mutate labels, change a PR, or create/update workflow state.

Prefer a snapshot passed into a validator over repeated network or filesystem reads. If snapshot sections are partial, expose that fact in `checks` and fail closed. A validation rule not currently exposed as a pure reader should be wrapped behind a narrow reader interface that reuses the same rule.

This interface keeps the next/check commands advisory while the existing completion contract remains authoritative. Any future stronger enforcement needs a separate decision informed by the evaluation plan and must preserve backward compatibility deliberately.

## Enforcement boundary

| Mechanism | What it can enforce | What it cannot establish |
|---|---|---|
| `workflow check` | Report whether the evidence visible to this invocation meets the selected structural and mechanical checks. | It cannot stop a user or agent from ignoring the result. It cannot prove a person read instructions or that semantic classifications are correct. |
| Existing `orchestune complete --result done` | Enforce current claim/worktree, clean state, CI evidence, PR, review judgments, review target SHA, and completion receipt conditions. | It cannot prove plan approval, reviewer choice was made at task start, impact-scope reasoning was sound, or CI ran before a PR was opened. |
| Existing CI workflow | Re-run local CI against a PR head before integration under current repository automation. | It does not enforce the full local sequence or advisory plan/scope requirements. |
| Direct `gh`, `git push`, or GitHub MCP writes | These remain available paths for users with credentials. | Local advice cannot prevent opening/updating a PR, pushing a branch, or writing comments through another client. The pre-push hook is not a workflow enforcement boundary. |
| CI evidence file | Existing evidence binds a result to recorded repository/worktree inputs and is checked by the completion validator. | The evidence file is not signed; manual fabrication or an environment override can bypass trust assumptions. Treat it as local process evidence, not a cryptographic attestation. |

Optional future work may validate PR description sections or reply markers server-side. That is not part of the first advisory rollout. Mechanical checks must also not claim to judge the substantive correctness of impact classifications; that remains a human/strong-model review candidate.

## Compatibility, migration, and implementation order

- Add `orchestune workflow` as an additive CLI command. Existing claim, review, complete, and CI commands retain their current behavior.
- Ship `orchestune/workflow/stages.toml` in wheels using the project's package-resource mechanism. Validate the installed-wheel path, not only source-tree execution.
- Newer workflow guidance may use `orchestune workflow next/check` when available. Skills first probe with `orchestune workflow --help`; if the probe exits nonzero or reports an unrecognized workflow command, log that the prose fallback was selected and retain the probe's error as diagnostic context. Do not infer that a workflow evidence result is a missing-subcommand signal. Absence of the new subcommand must not block established work.
- Treat unsupported state or TOML schema versions as a clear unmet/tool-version result. Do not silently reinterpret or rewrite user state.
- Preserve unknown JSON fields when a supported forward-compatible reader can do so safely; reject conflicting identity and ownership fields.
- Keep the initial-scope snapshot immutable. Schema evolution may add separate reconciliation metadata but must not alter the digest's source content.

Recommended follow-up order:

1. Measure the current workflow (evaluation plan, Issue #1493), in parallel with document and stage-contract work.
2. Add the stage definitions and loader, package the TOML resource, and reconcile duplicated skill/AGENTS prose (Issue #1495).
3. Implement read-only advisory `workflow check <gate>` against narrow readers and validators (Issue #1496).
4. Implement `workflow next` over the stage definitions and check results (Issue #1497).
5. Compare baseline and advisory arms using the linked evaluation plan; decide separately whether any stronger gate is justified.

## Acceptance-criteria crosswalk

The seven parent Issue acceptance criteria map to this document as follows:

| #1417 acceptance criterion | Covered by |
|---|---|
| Inventory current instructions and mechanical validation as implemented, missing, or requiring a decision. | [Current workflow and validation inventory](#current-workflow-and-validation-inventory). |
| Define state transitions, stage entry/exit, and evidence lifetime. | [Vocabulary](#vocabulary), [Evidence lifetime](#evidence-lifetime), and [Lifecycle](#lifecycle-interruption-and-retry). |
| Define next/check responsibilities, I/O, and minimum rollout. | [Gates](#gates-and-checks), [CLI contract](#cli-contract), [JSON result](#json-result), [Exit codes](#exit-codes), and advisory boundaries. |
| Preserve claim/review/complete integration and define interruption, approval wait, blocked, and retry handling. | [Lifecycle](#lifecycle-interruption-and-retry) and [Read-only architecture](#read-only-architecture-and-integration). |
| State bypass paths and the limits of enforcement. | [Enforcement boundary](#enforcement-boundary). |
| Define and record target model configurations for the comparison. | Summary and link to [`workflow-adherence-eval.md` §2.2, “Fix and record execution conditions”](workflow-adherence-eval.md#22-fix-and-record-execution-conditions); the detailed study plan is maintained separately. |
| Propose implementable follow-up issues, order, compatibility, and migration. | [Compatibility, migration, and implementation order](#compatibility-migration-and-implementation-order). |
