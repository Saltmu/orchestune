# Issue #966 implementation plan

## Preflight

- Issue: #966, existing issue (non-interactive workflow)
- Worktree: `claude/issue-966-task-966`
- GitHub backend: `gh` CLI (`gh auth status` succeeded)
- Reviewer: Codex (requested by the user)
- `uv lock --check`, `uv sync`, and the baseline record completed before implementation.
- Serena symbol indexing was unavailable in this environment. Impact enumeration uses `rg`/text search fallback, with dynamic/config/documentation searches recorded below.

## Design

Normalize only the two paths shared with claim at the dispatcher boundary, after CLI/config merging and before constructing `DispatcherConfig`:

1. Reuse `resolve_claim_workspace(cwd, explicit_state_path, explicit_worktree_root)`.
2. Carry its resolved absolute paths through `_DispatcherInputs`.
3. Store those paths in `DispatcherConfig`; leave log/event/not-needed paths unchanged.
4. Preserve repository-outside failure from `get_git_repository_paths` (fail closed).
5. Document the primary checkout root rule in English and Japanese usage docs.

## Impact scope (text-search fallback)

| Reference | Decision | Rationale |
| :--- | :--- | :--- |
| `orchestune/dispatch/dispatcher.py:_DispatcherInputs` | in scope | Carries resolved shared paths from the CLI boundary to config construction. |
| `orchestune/dispatch/dispatcher.py:_load_dispatcher_inputs` | in scope | Has the merged CLI/config values and the caller-provided `cwd`; this is the single normalization boundary. |
| `orchestune/dispatch/dispatcher.py:_build_dispatcher_config` | in scope | Must write resolved paths into `DispatcherConfig`. |
| `orchestune/dispatch/dispatcher.py:main` | in scope | Passes `cwd` into the normalization flow and preserves parser error handling. |
| `orchestune/claim/workspace.py:resolve_claim_workspace` | still out of scope | Existing primary-root contract is reused unchanged. |
| `orchestune/dispatch/config.py:DispatcherConfig` | still out of scope | Type/default contract remains unchanged; only construction inputs are normalized. |
| `orchestune/dispatch/cycle_actions.py:_bind_dispatch_claim_fn` | still out of scope | It already forwards `config.run_state_path` and `config.worktree_root`; normalized config makes those values consistent. |
| `orchestune/dispatch/launch.py` and GC/recovery consumers of `DispatcherConfig` | still out of scope | They consume config values and require no path-resolution logic once config is absolute. |
| `tests/test_dispatcher_shared_paths.py` | in scope | Verifies config-file and CLI-derived paths, primary/linked-worktree resolution, absolute preservation, and repository-outside failure. |
| `tests/test_dispatcher_cli_config.py` / `tests/test_dispatcher_cli_options.py` | in scope | Updated existing expectations/fixtures for the new repository-root contract. |
| `tests/test_dispatch_launch_claim_mapping.py` | still out of scope | Existing absolute-config forwarding coverage remains valid; the claim mapping code itself is unchanged. |
| `tests/test_claim_workspace.py` | still out of scope | Existing resolver contract is regression coverage and should remain unchanged. |
| `docs/en/usage.md` / `docs/ja/usage.md` | in scope | Publicly document relative path semantics for both CLI and config-file values. |

Supplementary searches covered `getattr`/`setattr`/`**kwargs`, string-based patches, serialized config keys, and docs/skills references for `run_state_path` and `worktree_root`; no additional dispatcher boundary required changes were found.

## Verification

- Add failing tests first for primary root, subdirectory, absolute values, config values, and outside-repository failure.
- Run focused pytest, then coverage/edge-case tests and baseline-aware local CI.
- Reconcile this impact table before review; any unplanned changed reference will be recorded in the PR body.

## Reconciliation

| Reference | Result |
| :--- | :--- |
| `_DispatcherInputs`, `_load_dispatcher_inputs`, `_build_dispatcher_config`, `main` | done — resolved paths are carried explicitly and written into `DispatcherConfig`. |
| `resolve_claim_workspace`, `DispatcherConfig`, claim/launch/GC consumers | still out of scope — reused/consumed unchanged. |
| dispatcher shared-path tests | done — primary, linked-worktree/subdirectory, config, absolute, and outside-repository cases covered. |
| claim workspace tests and claim mapping | still out of scope — existing tests pass unchanged; mapping receives config's normalized values. |
| English/Japanese usage docs | done — relative-path primary-root semantics documented. |

No unplanned production reference required changes. The only additional test edits were compatibility setup for pre-existing tests that intentionally place config fixtures outside Git; their workspace lookup is routed through the linked checkout, while the dedicated outside-repository test exercises the fail-closed behavior directly.

## #822 observation record

This task used the `rg`/text-search impact fallback because Serena was unavailable. Environment: Linux, Python 3.13.15, uv 0.12.13, ruff 0.4.10, mypy 1.20.2; base `origin/main` at the worktree claim point. Tokens: unavailable (no token counter exposed). Initial review record will be appended before requesting Codex review with the reviewed SHA and diff size.

## Initial review request record

| Field | Value |
| :--- | :--- |
| PR / status | #995 / ready for Codex review |
| Requested SHA | `ae363630c1ade8bb10061896eff6d4ba415bd51b` |
| Base | `origin/main` (`e7ebb0c`) |
| Initial diff | 7 files, +283 / -7 lines; behavior + regression tests + docs |
| Reviewer / provider | Codex via `scripts/wait_for_review.py` |
| Change type | Dispatcher startup path-contract feature/fix |

## Review round 1 reconciliation

Codex reported one P1 test portability finding: fixed-parent indexing (`parents[3]`) assumed this linked-worktree directory depth and fails in a standard checkout. The tests now derive linked and primary roots through `resolve_claim_workspace(Path.cwd())` and `common_dir.parent`; the production implementation was unchanged. This was a test-enumeration/fixture portability issue, not a production scope miss.
