# Contributing to Orchestune

[English](CONTRIBUTING.md) | [日本語](CONTRIBUTING.ja.md)

This document covers how to set up a local development environment for Orchestune itself. If you just want to *use* Orchestune in another project, see the [README](README.md) instead.

## Setup

Ensure you have Python 3.12+, Poetry, and the GitHub CLI (`gh auth status`) installed, then install dependencies:

```bash
poetry install
```

Then install the local Git pre-commit hook to prevent force-added `.gitignore` files from being committed accidentally (this also cleans up any legacy `pre-push` hook):

* **POSIX (Linux / macOS)**:
  ```bash
  ./scripts/setup-git-hooks.sh
  ```
* **Windows (PowerShell)**:
  ```powershell
  .\scripts\setup-git-hooks.ps1
  ```

`setup-git-hooks` also installs [gitleaks](https://github.com/gitleaks/gitleaks#installing) to `~/.local/bin` if it isn't already on your `PATH` (see `scripts/install-gitleaks.sh` / `.ps1`). `local-ci.sh` / `.ps1` retry this automatically too, so a missing `gitleaks` binary shouldn't block local CI execution in a fresh environment. If automatic installation fails (e.g. no network access, unsupported OS/architecture), install it manually from the link above.

## Code Analysis Tool (Serena MCP)

We use [Serena](https://github.com/oraios/serena) as an MCP server for pre-implementation impact analysis ([#822](https://github.com/Saltmu/orchestune/issues/822)). It provides type-aware symbol and reference search through a Python language server, so a field such as `depends_on` — which exists on several distinct types in this repository — can be tracked per type rather than as one undifferentiated text match.

The connection settings live in the repository at [`.mcp.json`](.mcp.json), pinned to `serena-agent==1.7.0`. Adoption is **optional**; every other development task works without it.

### Prerequisite

[uv](https://docs.astral.sh/uv/getting-started/installation/) is required, since `.mcp.json` launches Serena through `uvx`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Afterwards, restart your agent session inside the repository (or inside a worktree under it) and the project-scoped MCP server is picked up. MCP servers are loaded only at session start, so a change to `.mcp.json` requires a session restart.

### Worktrees and the index

Because `--project-from-cwd` is set, Serena walks up from the current directory and resolves the project root to the nearest ancestor holding either `.serena/project.yml` or `.git` (**including a git worktree pointer file**). A session started in `worktree/<BRANCH_SLUG>/` therefore roots at that worktree itself, and indexes are never shared between worktrees.

The symbol cache is stored under `<project root>/.serena/cache/<language>/` and keyed as `relative file path → (content hash, symbols)`. Entries whose hash no longer matches are discarded and re-requested from the language server, so switching branches cannot serve symbols from an older commit. No manual re-index step is needed; only if results look plainly wrong, delete that worktree's `.serena/cache/`.

`.serena/` (index, cache, memories) is git-ignored. Do not commit it.

### Fallback and opt-out

If the MCP server fails to start or the language server stops responding, fall back to the existing text search (`rg` / ripgrep, `grep`), record that in `implementation_plan.md`, and keep working. Text search cannot separate identically named symbols by type, so widen your review accordingly. Never stall a task on tooling trouble.

To opt out permanently, disable the MCP server on the client side (in Claude Code, via `claude mcp` configuration or by disconnecting from `/mcp`).

For how to enumerate, classify, and reconcile the impacted sites, see [`skills/local-ci-developer/references/impact-scope.md`](skills/local-ci-developer/references/impact-scope.md).

## Running Tests

Execute the full test suite using `pytest`:
```bash
poetry run pytest
```

### Test suite markers

Every collected test belongs to exactly one suite:

- `unit`: an isolated module, class, or function tested with fakes/mocks and no real infrastructure boundary.
- `integration`: coordination across components or a real boundary such as git, subprocesses, or file locks.
- `e2e`: a public CLI or major workflow exercised from its entry point through the resulting outcome.

Tests without a more specific marker default to `unit`. The collection guard rejects conflicting suite markers, while `uses_real_file_lock` remains an orthogonal execution-characteristic marker.

Run a selected suite with strict marker validation:
```bash
poetry run pytest --strict-markers -m unit
poetry run pytest --strict-markers -m integration
poetry run pytest --strict-markers -m e2e
```

Coverage instrumentation is intentionally left out of the default `pytest` run to keep the local dev loop fast. To check coverage, pass the flags explicitly (this is also what `local-ci.sh` runs):
```bash
poetry run pytest --cov=orchestune --cov-branch --cov-report=term-missing
```

### Mocking Internal Symbols

When `unittest.mock.patch(...)` / `patch.object(...)` targets an `orchestune.` or `scripts.` symbol (as opposed to a process/OS boundary such as `subprocess`, `time.time`, or `os.kill`, or a test double such as `FakeForge`), pass `autospec=True` ([#829](https://github.com/Saltmu/orchestune/issues/829)). Without it, `patch(...)` still catches a *renamed or removed* symbol (`AttributeError`), but not a *changed signature*: a mock silently accepts whatever arguments the caller passes, so a positional argument added to, removed from, or reordered on the real function does not fail the test.

```python
# Catches a rename, but not a changed argument count/order:
with patch("orchestune.dispatch.worktree._branch_exists") as mock_exists:
    ...

# Also catches a changed signature, since the mock is built from the real
# function's signature and rejects calls the real function would reject:
with patch("orchestune.dispatch.worktree._branch_exists", autospec=True) as mock_exists:
    ...
```

`autospec=True` does not apply, and should be omitted, when:
- the target is a process/OS/library boundary already covered by a different exclusion (`subprocess`, `os.kill`/`environ`/`getpid`, `time.time`/`sleep`/`monotonic`, `shutil`, `urllib`, `fcntl`, `msvcrt`, `pathlib.Path.cwd`/`home`) or a test double (`FakeForge`, `fake_forge_proxy.*`, a `MagicMock`-backed fixture) — these are boundaries or fakes by design, not internal contracts to verify;
- the call supplies its own replacement via `new=`/`new_callable=` (there is no real signature for autospec to derive, since the caller controls the substitute object directly);
- the target is a non-callable attribute (e.g. `__file__`) — autospec is for callables;
- `create=True` is required because the target is a builtin or a platform-conditional attribute not always present on the module (e.g. `open`, a Windows-only `ctypes` handle) — autospec would need the attribute to exist to introspect it.

## Local CI Script

Before committing or pushing your changes, run the local CI script to verify formatting, types, and tests:
* **POSIX (Linux / macOS)**:
  ```bash
  ./scripts/local-ci.sh
  ```
* **Windows (PowerShell)**:
  ```powershell
  .\scripts\local-ci.ps1
  ```
This runs:
1. **Ruff Format & Lint Check**: `ruff format` and `ruff check`
2. **Mypy Type Check**: Type hint validation
3. **Pytest Coverage Check**: Ensures coverage does not drop below 90%
4. **Secret & Local Path Scan** (`gitleaks`): Blocks commits/pushes that leak secrets or absolute local paths (e.g. `file:///home/<user>/...`). Config lives in [`.gitleaks.toml`](.gitleaks.toml). `local-ci.sh` / `.ps1` auto-install gitleaks if it's missing (see `scripts/install-gitleaks.sh` / `.ps1`); if that installation fails, the script fails (rather than skipping) so this check is always enforced before you can push. It's also re-checked in CI as a backstop.
