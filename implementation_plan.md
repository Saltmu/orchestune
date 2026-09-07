# Implementation Plan: Issue #842 (`orchestune --version`)

## 0. Preflight & Execution Environment

- Target Issue: #842
- Parent Issue: #825
- Base Branch: `parent/issue-825` (`253bac4`)
- Task Branch: `feat/issue-842-cli-version`
- GitHub backend: `gh` CLI (authenticated; no GitHub MCP used)
- Serena: symbol lookup and reference enumeration available after activating the task worktree
- OS: Linux
- Planned verification: focused pytest, isolated CLI smoke test, then `./scripts/local-ci.sh`

## 1. Impact Scope Determination

The changed production symbol is `orchestune.cli.main`. Serena symbol lookup and reference
enumeration were used; supplementary `rg` searches covered dynamic argv handling, string-based
patches, entry points, version references, and documentation.

| Reference | Decision | Rationale |
| --- | --- | --- |
| `orchestune/cli.py:main` | in scope | Must recognize `--version` before subcommand delegation. |
| `orchestune/version.py:get_version` | out of scope | Existing single version source is consumed read-only; no behavior change required. |
| `orchestune/__init__.py:__version__` | out of scope | Existing public API already delegates to `get_version`; changing it would duplicate the fix. |
| `pyproject.toml:[project.scripts]` | out of scope | The `orchestune = orchestune.cli:main` entry point is correct and needs no metadata change. |
| `tests/test_cli.py` | in scope | Add regression coverage for the new top-level option while preserving delegation tests. |
| `tests/test_placeholder.py:get_version` | out of scope | Covers the version source itself, not CLI argument dispatch. |
| `tests/test_skill_commands.py` | out of scope | Its `--version` references concern interpreter command parsing, not the `orchestune` entry point. |
| `docs/en/setup.md`, `docs/ja/setup.md` | out of scope | Existing `claude --version` text documents another CLI and is unrelated to this entry point. |
| `scripts/`, `.github/workflows/` | out of scope | No script or workflow command contract changes are needed for a top-level read-only flag. |

## 2. Design

1. Import `get_version` directly from `orchestune.version` in the L4 CLI module.
2. Handle `--version` and the conventional `-V` alias before rewriting `sys.argv` for delegated subcommands.
3. Print `orchestune <version>` and return successfully.
4. Add focused unit tests for both supported flags and retain the existing unknown/no-argument behavior.

## 3. TDD / Verification Plan

- Red: add the CLI flag tests and verify they fail on the current implementation.
- Green: implement the minimal dispatch branch and rerun the focused tests.
- Smoke: build/install the wheel into an isolated uv environment and run `orchestune --version` and `orchestune-dispatch --help`.
- Full: run `uv lock --check`, `uv sync`, and `./scripts/local-ci.sh`.

## 4. TDD Results

- Red: `uv run pytest tests/test_cli.py -q` — 1 failed, 11 passed (`--version` was unknown).
- Green: `uv run pytest tests/test_cli.py -q` — 13 passed.
- Smoke: `uv build` succeeded; an isolated wheel install printed `orchestune 0.5.0`, and `orchestune-dispatch --help` exited successfully.
- Impact scope: all in-scope references are addressed; out-of-scope rationales remain valid.
