# Implementation Plan: Issue #844

## Preflight (Step 0)
- Tooling: `uv 0.12.10` available; `uv sync` succeeded in the task worktree.
- GitHub backend: `gh` CLI authenticated (`gh auth status` OK) → use `gh` for Issue/PR operations.
- gitleaks 8.30.1 available.

## Branch base
This worktree branches from `origin/parent/issue-825` (not `main`), because
issue #844 is a follow-up defect discovered *after* the Poetry→uv migration
(#825) landed on that parent branch. On `main`, the migration has not landed
yet (`poetry.lock` still present, no `uv.lock`), so the bug does not exist
there yet. `orchestune/dag/contracts.py` (`_SHARED_CONTRACT_PATTERNS`,
`dependency-manifest` category) and `orchestune/dispatch/locks.py`
(`_HOTSPOT_PATTERNS`) already list `uv.lock` symmetrically with `poetry.lock`
on `parent/issue-825` — confirmed by reading both files on that branch. Only
`orchestune/dag/models.py`'s `_IGNORED_FOOTPRINT_PATTERNS` was missed, exactly
as the issue body states.
PR base: `parent/issue-825` (Parent Issue Mode, per pr.md precedence rules).

## Root cause
`orchestune/dag/models.py:13-20` (`_IGNORED_FOOTPRINT_PATTERNS`), consumed by
`is_ignored_footprint()` / `SubTask.touch_set()`, ignores `poetry.lock` but
not `uv.lock` when computing the touch-set used for similarity Conflict Edge
scoring and heuristic shared-contract-hotspot conflicts. Two tasks sharing
only `uv.lock` therefore produce a conflict where two tasks sharing only
`poetry.lock` do not — reproduced directly:

```
is_ignored_footprint('poetry.lock') -> True
is_ignored_footprint('uv.lock')     -> False
```

## Fix
Add `re.compile(r"(^|/)uv\.lock$")` to `_IGNORED_FOOTPRINT_PATTERNS` in
`orchestune/dag/models.py`, keeping `poetry.lock` for backward compatibility.

## Impact scope (Step 2.6)

Serena MCP was unavailable in this environment for this quick, single-symbol
change; falling back to text search (`rg`) across the repo, per the fallback
procedure in impact-scope.md.

| Reference | Decision | Rationale |
| :--- | :--- | :--- |
| `orchestune/dag/models.py:_IGNORED_FOOTPRINT_PATTERNS` | in scope | the tuple being fixed |
| `orchestune/dag/models.py:is_ignored_footprint` / `SubTask.touch_set` | out of scope | consumes the tuple generically; no change needed, only the data it reads changes |
| `orchestune/dag/contracts.py:_SHARED_CONTRACT_PATTERNS` (dependency-manifest) | out of scope | already includes `uv.lock` on this branch (verified by reading the file); different purpose (writer-category detection, not conflict-ignore), docstring explicitly says the two lists are intentionally separate |
| `orchestune/dispatch/locks.py:_HOTSPOT_PATTERNS` | out of scope | already includes `uv.lock` on this branch (verified by reading the file); different purpose (dispatch-time churn suppression), docstring explicitly says it does not share patterns with the DAG module |
| `tests/test_dag_contracts.py::TestCategorize` | out of scope | already asserts `_categorize("uv.lock") == "dependency-manifest"` |
| `tests/test_dispatch_locks.py` | out of scope | already has extensive `uv.lock` hotspot coverage |
| `tests/test_dag_graph.py` | in scope | no existing regression test pins `_IGNORED_FOOTPRINT_PATTERNS` symmetry; add one |
| `docs/en/usage.md:80,180`, `docs/ja/usage.md:80,180` | in scope | both list the built-in ignore list / dependency-manifest list in prose; line 80 (dependency-manifest) already matches code (`contracts.py` includes uv.lock) but doc text omits it; line 180 (built-in ignore list) omits `uv.lock` entirely, matching the code bug |
| `orchestune/dag/similarity.py`, `orchestune/dag/graph.py` | out of scope | consume `touch_set()` output generically; no literal `poetry.lock`/`uv.lock` reference (confirmed via `rg`) |

Supplementary text search run: `rg -n "poetry\.lock|uv\.lock" orchestune tests docs` across the worktree (see below), plus explicit `git show` reads of `orchestune/dag/contracts.py` and `orchestune/dispatch/locks.py` on this branch to confirm their current state before excluding them.

## Tests to add
- `tests/test_dag_graph.py`: a new test class asserting `SubTask.touch_set()`
  excludes `uv.lock` the same way it excludes `poetry.lock` (parametrized),
  plus a `build_dag`-level symmetry test: two tasks sharing only `uv.lock`
  produce the same (no-conflict) result as two tasks sharing only
  `poetry.lock`, and an explicit `shared_contract` writer conflict on
  `uv.lock` is unaffected (still conflicts).

## Verification
```bash
uv run pytest tests/test_dag_graph.py tests/test_dag_contracts.py tests/test_dispatch_locks.py
./scripts/local-ci.sh
```

## Reviewer bot
Claude-authored task → Codex reviewer (per Step 1 default resolution).
