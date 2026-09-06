# Issue #834 Implementation Plan

## Preflight

- Worktree: `worktree/parent-issue-834` on the user-specified branch
  `parent/issue-834`, based on `origin/parent/issue-825` because #834 depends
  on the packaging migration in #833.
- GitHub backend: authenticated `gh` CLI (fixed for this task).
- Reviewer: Claude, resolved for a Codex-dispatched task.
- `poetry --version` and `gitleaks version` succeeded. `poetry check --lock`
  could not be retained after the #833 dependency migration because this base
  intentionally has no `poetry.lock`; `uv --version` succeeded instead.
- Serena MCP is unavailable in this session, so impact enumeration used `rg`,
  including searches for dynamic access, mock patch strings, configuration and
  documentation references.

## Design

Replace Poetry command invocations at the local execution boundary with `uv`.
The local CI scripts will require `uv`, run `uv sync` to make the worktree
environment current, and run every quality gate through `uv run`. The baseline
formatter will use the same runner. The integrator boundary will use `uv sync`
and resolve the repository-local `.venv`, preserving existing nearby `.venv`
fallback behavior for worktree execution.

## Impact scope

| Reference | Decision | Rationale |
| :--- | :--- | :--- |
| `scripts/local-ci.sh` | in scope | Owns Linux CI tool detection, dependency preparation, and all quality-gate commands. |
| `scripts/local-ci.ps1` | in scope | Owns the equivalent Windows CI tool detection, preparation, and gates. |
| `scripts/ci_baseline.py:RUFF_FORMAT_COMMANDS` | in scope | Supplies the formatter commands executed by the baseline wrapper. |
| `tests/test_ci_baseline.py` | in scope | Asserts the formatter command contract and execution order. |
| `orchestune/infra/python_env.py:install_dependencies` | in scope | Integrator's worktree dependency-installation boundary. |
| `orchestune/infra/python_env.py:resolve_virtualenv_path` | in scope | Resolves the environment made by `uv sync` before the integrator runs CI. |
| `orchestune/integrator/git_ops.py:_prepare_ci_environment` | out of scope | Delegates to the two changed L1 functions without embedding Poetry or virtualenv semantics. |
| `tests/test_python_env.py` | in scope | Verifies installation errors and virtualenv resolution at the changed boundary. |
| `tests/test_integrator_git_ops.py:TestRunCiVenvDetection` | out of scope | Mocks the L1 functions and asserts delegation, which remains unchanged. |
| `tests/test_integrator_step_merge.py:TestCiEnvironment` | in scope | Full-suite reconciliation found it asserted the replaced Poetry subprocess and its managed-venv lookup; it now verifies `uv sync` and the repository `.venv`. |
| `tests/test_scripts.py` | in scope | Statically enforces the shell and PowerShell CI command contracts. |
| `tests/test_ci_workflow.py:test_local_ci_sh_does_not_bypass_pytest_worker_cap` | in scope | Inspects the CI pytest invocation and must recognize `uv run pytest`. |
| `pyproject.toml`, `uv.lock` | out of scope | #833 already supplies the uv metadata and lockfile; #834 consumes them only. |
| documentation and skills containing `poetry` | out of scope | The Issue footprint is execution and worktree behavior; these instructions are not executed by the affected runtime paths. |

Supplementary searches covered `getattr`/`setattr`, `**kwargs`, patch strings,
serialized/configuration names, and all textual `poetry`/`uv` command
references. No dynamic access or string-addressed mock targets reference the
changed `python_env` functions beyond the listed tests.

The `tests/test_integrator_step_merge.py` reference was an enumeration miss: it
was discovered by the full-suite run, then added as in scope before its contract
test was updated.

## TDD and verification

1. Add expectations for `uv sync`, `uv run`, and `.venv` resolution, then run
   the focused tests to demonstrate the pre-change failure.
2. Implement the minimal command and resolver changes.
3. Run the focused Issue verification, then `uv run pytest` with coverage and
   `./scripts/local-ci.sh` via the baseline wrapper.
4. Reconcile this table, create a PR against `parent/issue-825`, run the
   automated Claude review loop, and post the required outcome record.
