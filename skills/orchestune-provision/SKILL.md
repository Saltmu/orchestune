---
name: "orchestune-provision"
description: "Internal follow-up skill invoked by orchestune once a decomposition plan is approved: creates GitHub Issues for each subtask using the orchestune provision CLI (or manual fallback)."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Provision Skill

This skill takes an **approved `decomposition_plan.md` from the `orchestune` skill** and files each subtask as a GitHub Issue via the `orchestune provision` CLI, wiring up the parent/child and dependency relationships.

## Trigger conditions

**This is not normally a skill users invoke directly.** The [orchestune skill](../orchestune/SKILL.md) loads it internally as a handoff once a decomposition plan has been approved.

As an exception, a human may load this skill directly if they only want to manually run Issue provisioning against an unfiled `decomposition_plan.md`.

## Prerequisites

* The `orchestune` CLI tool (`orchestune provision`, `orchestune bootstrap`) must be installed on the system.
* Run `orchestune bootstrap` before provisioning starts to confirm `gh` authentication and the required labels exist (see step 1).

## Workflow: Issue provisioning

Filing Issues from `decomposition_plan.md` is fully codified into the `orchestune provision` command (#306). Given an approved plan, provisioning is a deterministic transformation — no agent needs to interpret the procedure.

1. **Pre-flight**: Run `orchestune bootstrap` to confirm `gh` authentication and the presence of the required labels (`status:*`, `priority:*`, `risk:flagged`, `progress:partial`, `not-needed-review:*`), filing any that are missing. If it fails (exit 1), stop here and follow its guidance (e.g. set up authentication) before retrying.
2. **Preview**: Check the content before writing anything.
   ```bash
   orchestune provision --plan decomposition_plan.md --no-apply
   ```
   This prints each Issue's title, labels, and body that would be created, without writing to GitHub.
3. **Provision**: If it looks right, apply it for real.
   ```bash
   orchestune provision --plan decomposition_plan.md
   ```
   This files the parent Issue (`[EPIC] <title>`) from `title`, then files each subtask Issue in the topological order of `decomposition_plan.md`'s `depends_on`, setting the `--parent`/`--blocked-by`-equivalent relationships via `gh issue edit --set-parent`/`--add-blocked-by`. Each filed Issue number is written back into `decomposition_plan.md`'s frontmatter (`parent_issue_number`, and each subtask's `issue_number`) as it's created, and the entire updated plan is synchronized into the parent Issue's body under `<!-- orchestune:decomposition-plan -->`. **Idempotent and resumable from a partial failure**: a subtask whose `issue_number` is already set, or whose `subtask_id` matches a Footprint YAML block embedded in an existing child Issue under the parent, is not recreated — the existing Issue number is reused as-is. Each subtask Issue's Footprint YAML also always embeds the parent number (`parent_issue_number`), so even in a degraded environment where `add_sub_issue`/`set_blocked_by` fails (e.g. no native-relationship support), a `--parent-issue`-mode Dispatcher can still discover the target Issue via this body metadata, and `orchestune provision` itself completes without aborting (degraded subtasks are reported via `ProvisionResult.degraded_subtask_ids`). See the docstrings in `orchestune/provisioning.py` and `docs/ja/usage.md` for the exact derivation rules (label rules, `.github/issue_template.md` placeholder substitution, etc.).

   **Attaching to a pre-existing EPIC Issue**: if the EPIC Issue was already filed ahead of time (by hand, or via plain GitHub — not by Orchestune), pass `--parent-issue <N>` instead of relying on `title` to create/reuse one:
   ```bash
   orchestune provision --plan decomposition_plan.md --parent-issue <N>
   ```
   The target Issue is normalized in place if it doesn't already look like an Orchestune EPIC (a `"[EPIC] "` title prefix and parent marker are added as needed, preserving its existing content) — no title match against the plan's `title` is required. `--parent-issue` must be passed on **every** `provision` (and `dispatch`) run for this plan: because a pre-existing Issue's title generally won't match the plan-derived `"[EPIC] " + title`, the persisted `parent_issue_number` alone can't be auto-recognized on a later run without the flag.

   **Restoring a lost plan file from parent**: if `decomposition_plan.md` was lost (e.g. after worktree cleanup), restore it directly from the parent Issue:
   ```bash
   orchestune provision --restore-plan <parent_number>
   ```
4. **Return the list of filed Issues to the [orchestune skill](../orchestune/SKILL.md)**, for it to report to the user or hand off to the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md).

### Fallback for environments without `gh` (manual filing)

`orchestune provision` calls the `gh` CLI internally. In an environment where `gh` itself can't be installed or authenticated, use the GitHub MCP server, or guide the user through manual filing via the Web UI instead. In that case, replicate the following mapping by hand:

* Parent Issue: file it with the title `[EPIC] <title>` from `decomposition_plan.md`'s `title`, then write the resulting number back into `parent_issue_number`.
* Each subtask Issue: fill `.github/issue_template.md`'s placeholders (`{{subtask_id}}`, `{{subtask_id_yaml}}`, `{{description}}`, `{{overview}}`, `{{proposed_changes}}`, `{{acceptance_criteria}}`, `{{verification_plan}}`, `{{footprint}}`, `{{symbols}}`, `{{depends_on}}`, `{{parent_issue_number}}`) from the subtask's fields, then write the resulting number back into that subtask's `issue_number`. `{{subtask_id}}` is for display use (headings, etc. — the raw value), while `{{subtask_id_yaml}}` is for use inside the Footprint YAML block only (a YAML-scalar-quoted value that's safe even for IDs containing `:` or `#`) — always use the correct one for its context. `{{parent_issue_number}}` is the parent Issue's number (`null` if not yet resolved); don't omit it, since it's the required fallback the Dispatcher uses to discover child Issues in environments where native relationships aren't available.
* **Always set labels**: `dispatch_cycle._group_by_status` never picks up an Issue that has no status label (`status:queued`/`status:blocked`/etc.), so forgetting to label a subtask means it's never dispatched, permanently. Set `status:queued` if `depends_on` is empty or every dependency is already done, otherwise `status:blocked`. Also always set `priority:{subtask.priority}` (from `decomposition_plan.md`'s `priority`; `medium` if unset), and `risk:flagged` if `risk` is true (these derivation rules mirror `_derive_labels` in `orchestune/provisioning.py`).
* Filing Issues via the GitHub MCP may not be able to set native `blocked_by`/`parent` relationships. Even so, always preserve Footprint YAML's `depends_on` — the Dispatcher uses this value for dependency resolution and for restoring branch stacking during self-healing. If you want the GitHub-native relationships visible too, add them after filing via the Web UI or `gh issue edit --set-parent`/`--add-blocked-by`.
