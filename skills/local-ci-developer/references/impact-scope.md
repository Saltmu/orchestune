# Impact Scope Determination (Step 2.6)

Run this **inside the task worktree, before writing any production code** to refine the [initial footprint](../SKILL.md#initial-footprint-before-issue-creation-or-claim) declared before Issue creation/claim. Reconcile the detailed findings with that declaration and the held reservation; follow the linked procedure if they differ.

Start: matching claim/worktree and initial footprint. Actions: enumerate, classify and preserve the initial table below. Finish: its reference/decision/rationale rows and immutable snapshot are recorded. Failure: use text-search fallback for unavailable tools; reconcile ownership/scope conflicts before editing.

## Plan marker (schema v1)
During preparation the executor writes exactly one locator in `implementation-plan.md`:
````markdown
```orchestune-workflow
schema_version: 1
state: workflow-state.json
```
````
The state path is relative to the plan and must remain within the explicit session directory (no absolute/escaping paths). Parse with safe YAML and only these scalar keys. Zero markers means absent; multiple markers, unknown keys/version or identity conflicts mean unmet, never approval. The [state contract](../../../docs/plans/local-ci-workflow-enforcement.md#persisted-workflow-context-and-plan-marker) owns its schema; readers never create/rewrite state.
Digest the exact initial table only as UTF-8, LF, one final newline; record its relative path/digest in `impact_initial`. Preserve the initial snapshot before production edits and append reconciliation separately, never rewrite it.

During #822 observation, start the [measurement record](measurement.md) before preparation
or searches. Preserve the original table before implementation; append reconciliation
and review discoveries separately rather than rewriting the original decisions.

## 1. Enumerate

List the functions, classes, fields, constants, config keys and CLI options you intend to change; for a CLI option, also find tests that pin the parser's option set or groups (e.g. `tests/test_dispatcher_cli_options.py`) and add them to the footprint.
Collect references with Serena (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`),
which distinguishes same-named fields on different types. If unavailable, use `rg` / `grep`,
record the fallback and manually distinguish same-named symbols; never skip investigation.

## 2. Cover what an index cannot see

A symbol index only knows static references. Before concluding the list is complete, also
search as text for:

- dynamic access: `getattr` / `setattr` / `**kwargs` / dict-keyed dispatch
- string-addressed test doubles: `patch("pkg.mod.symbol")`, fixture and helper names
- serialized names: JSON/YAML/TOML keys, GitHub Issue/PR body markers, label strings
- documentation and skill instructions that state the same contract in prose

**An empty search result is not proof of no impact.** It only means the index found
nothing; record which supplementary searches you ran.

## 3. Classify

Write the result into `<session-dir>/implementation-plan.md` as a table, one row per reference site:

| Reference | Decision | Rationale |
| :--- | :--- | :--- |
| `orchestune/x/y.py:_fn` | in scope | reads the renamed field directly |
| `orchestune/a/b.py:_g` | out of scope | consumes the derived value, not the field |

Every `out of scope` row needs a short reason. "Looks unrelated" is not a reason. If you
cannot justify exclusion in one sentence, treat the site as in scope.

Carry the same table into the PR body under the Walkthrough section.

## 4. Reconcile after implementation

Before requesting review, revisit the table and mark each row:

- **done** — changed as planned
- **still out of scope** — the original rationale survived implementation
- **unverified** — you could not confirm either way; state this explicitly in the PR

Any site you had to change that was *not* in the original table is a miss in the
enumeration; any site you had marked `out of scope` and then had to change is a miss in
the classification. Note which of the two occurred in the PR body — the distinction is
what makes the practice measurable.

## Failure handling

| Symptom | Action |
| :--- | :--- |
| MCP server does not start | Continue with `rg` / `grep`, note the fallback in `<session-dir>/implementation-plan.md` |
| Language server is slow on first call | Wait; the symbol cache is per-file and content-hashed, so later calls are cheap |
| Results look stale after a branch switch | Cached entries are keyed by file content hash and are discarded on mismatch; if results still look wrong, delete `.serena/cache/` in the worktree |
| Tool cannot be repaired within the task | Disable it by removing the server from the client session and proceed with text search; do not block the task on tooling |

`.serena/` is git-ignored. Never commit the index, cache, or memories.
