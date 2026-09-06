# Impact Scope Determination (Step 2.6)

Run this **inside the task worktree, before writing any production code**. The goal is to
decide *what to change* from evidence rather than from memory of the codebase.

## 1. Enumerate

List every symbol you intend to change: functions, classes, dataclass fields, constants,
config keys, CLI options.

For each one, collect its references with the Serena MCP tools (`find_symbol`,
`find_referencing_symbols`, `get_symbols_overview`). They are type-aware, so a field named
`depends_on` on one dataclass is not confused with an identically named field on another.

If the MCP server is unavailable, fall back to `rg` / `grep` and say so in the plan. A
text search over-reports across same-named symbols on different types, so widen the
manual review accordingly. Never silently skip this step.

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

Write the result into `implementation_plan.md` as a table, one row per reference site:

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
| MCP server does not start | Continue with `rg` / `grep`, note the fallback in `implementation_plan.md` |
| Language server is slow on first call | Wait; the symbol cache is per-file and content-hashed, so later calls are cheap |
| Results look stale after a branch switch | Cached entries are keyed by file content hash and are discarded on mismatch; if results still look wrong, delete `.serena/cache/` in the worktree |
| Tool cannot be repaired within the task | Disable it by removing the server from the client session and proceed with text search; do not block the task on tooling |

`.serena/` is git-ignored. Never commit the index, cache, or memories.
