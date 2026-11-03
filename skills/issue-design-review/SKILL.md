---
name: "issue-design-review"
description: "Verify the detailed design of an Orchestune Issue (feature, fix, or refactoring) against the call graph of existing code and tests, rewrite the Issue body, and record the changes in an Issue comment."
---

# Issue Detailed Design Review

Start from a design draft (for example one written by Grok) that says what goes where and what changes, and confirm it holds up on the existing code. The direction is usually right; what goes wrong are points you only see by following calls, so enumerate the items below mechanically.

This applies to every design that touches existing code: refactorings (behavior preserved) as well as features and fixes (behavior changed). Where the emphasis differs by kind, it is stated.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, rewritten Issue bodies, comments, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese). The language of this instruction document must not determine the output language.

## 0. Preparation

- Run `git fetch origin main` and note main's short SHA (write it in the body and the comment).
- Read the Issue body and the body and latest comments of related Issues (Blockers, Issues touching the same files). Check whether a related Issue is a parent EPIC (sub-issue style).
- List the existing classes, functions, and modules the design touches. Even for a design that only adds new files, list the existing touchpoints: callers, registries, configuration.

## 1. Enumerate references of the code you touch

For each method of a class you change, move, or split, list its `self.*` references and module-global references. Passing the design's grouping as arguments makes references that cross groups visible.

```python
# xref.py <path> <ClassName> <group0 names,comma> <group1 ...>
import ast, sys
path, cls = sys.argv[1], sys.argv[2]
groups = [set(g.split(",")) for g in sys.argv[3:]]
tree = ast.parse(open(path).read())
mod = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
mod |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
mod |= {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == cls:
        for m in node.body:
            if not isinstance(m, ast.FunctionDef):
                continue
            selfs = sorted({n.attr for n in ast.walk(m) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"})
            globs = sorted({n.id for n in ast.walk(m) if isinstance(n, ast.Name) and n.id in mod})
            g = next((i for i, s in enumerate(groups) if m.name in s), "keep")
            print(f"[{g}] {m.name} self={selfs} glob={globs}")
```

Check:

- **Call-backs**: does the new side call methods of the original side? If so, move those methods too, or pass values or callables as arguments.
- **Shared private types and constants** left in the original module make imports from the new module circular. Move them to a lower module and re-export them from the original one.
- **Mutable state**: do reads and writes share the same dict/set? If so, pass the same object and never rebind the attribute.
- **Values updated midway** (e.g. reassigning `self._now`). If a value is updated, pass a callable instead of a fixed value.
- **For features and fixes**: which existing read and write paths do the new branches or state go through? Is the same decision duplicated elsewhere?

## 2. Inheritance and overrides

- Find subclasses with `grep -rn "(<ClassName>)" orchestune`.
- Where the original class calls `self.x()` for a method a subclass overrides, the call goes through the override. Moving the implementation to another object bypasses the override. When adding behavior to a base class, also check that an override does not cancel it.
- The same applies wherever `self` is passed to a Protocol (such as `assess_x(deps, self)`).

## 3. Coupling with tests

- Count private methods tests call directly with `grep -rnoE "(obj|ClassName)\._[a-z_]+" tests`. If you rename them or change their signatures, put the test rewrites in the Footprint. When a refactoring moves them, keep delegates only for names that are called (none for names that are not). Watch for tests that build an object with `__new__` and set only some attributes.
- Find `patch.object(obj, "name")` / `monkeypatch.setattr(Class, "name")`. Keep the internal call path so instance patches still apply, or retarget the patches.
- Count string patches `patch("orchestune.<mod>.<name>")`. Retarget them if the call starts going through another module. When one call spans two modules and consumes one `side_effect` sequence in order, apply the same mock to both.
- Remove imports that become unused (old patch targets always fail with `AttributeError`).
- Enumerate public names other modules and tests import; if you move them, re-export them via `__all__` (for ruff `F401`).
- **For features and fixes**: list the existing tests whose expectations change under the new behavior, and write them in the body. Put at least one test per new branch in the acceptance criteria.

## 4. Mechanical repository checks

- ruff `select` in `pyproject.toml`: `SLF001` is enabled, so methods called from another object must be public. Also check `C90` (max-complexity). Once #1589 lands, check argument counts against `PLR0913` (`max-args = 9`, excluding `self`).
- `EXPECTED_LAYERS` in `tests/architecture_layers_support.py`. Decide each new module's layer and register it. A lower layer cannot import a higher one. Check whether the layer tables in `docs/*/architecture.md` use wildcards (`pkg.*`).
- `EXPECTED_SUBPROCESS_COMMAND_MODULES` in `tests/test_architecture.py` (modules allowed to run `gh` / `git`). New external commands go through an L1 adapter.
- `PRODUCTION_EXCEPTIONS` and `REMOVED_CONTEXT_ATTRIBUTES` in `tests/dependency_boundary_test_support.py`. Rename the module of exceptions for moved code, and do not keep unused exceptions.
- When adding or moving label transitions: the `file::Class.method` registrations in `tests/test_status_transition_callsites.py` and `docs/*/status-labels.md`.
- `[tool.detect-bloat]` in `pyproject.toml` (function 50 SLOC, file 1,000 SLOC). Check whether additions push a file toward the limit.
- When adding a setting or CLI flag: the config schema, the wizard, `orchestune.toml.example`, and the docs in both English and Japanese.

## 5. Dependencies and ordering

- Enumerate Issues touching the same files. Overlapping Footprints make claims exclusive. If an order is required, write it as a Blocker.
- If a Blocker is a parent EPIC (sub-issue style), state that the native blocked-by will be moved to the target sub-issue once it is filed. A standalone Issue keeps Footprint `depends_on: []`.
- Limit the Footprint to files you actually edit. Tests you do not rewrite belong under verification.

## 6. Rewriting the body and commenting

- Rewrite the body keeping its existing headings (overview / design / acceptance criteria / verification / out of scope / ordering / Footprint). Put placement and naming mappings in a table, with a one-sentence reason each (callers, tests, lint).
- Record the changes in an Issue comment using [the comment template](resources/comment-template.md).
- After writing, recount every number in the body (method counts, patch counts, number of re-exported names).
