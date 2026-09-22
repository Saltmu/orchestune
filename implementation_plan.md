# Issue #964 implementation plan

## Preflight & Environment

- Issue: #964, existing issue (non-interactive workflow)
- Worktree: `/home/micro/orchestune/worktrees/claude-issue-964-task-964`
- Base: `origin/main` (commit `d860f56`)
- GitHub backend: `gh` CLI (`gh auth status` verified)
- Reviewer: Claude (Codex and agy targets -> Claude per `local-ci-developer`)
- Preflight checks: `uv --version` (0.12.10), `uv lock --check`, `gitleaks version` (8.30.1) verified.
- Symbol indexing tool: Serena MCP (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`). Supplementary text searches via `git grep` for dynamic access, string-addressed test doubles, and serialized fields.

## Design

1. **既存 finding の不変**:
   - `HANDLELESS_EXECUTION_ORPHAN`（worktree 不存在必須）の定義・挙動は維持する。
   - 新規 finding `DISPATCH_PRELAUNCH_ORPHAN = "execution.dispatch-prelaunch-orphan"` を追加。
   - `orchestune/consistency/repairs/execution.py` で `COMMAND_RECLAIM` と `COMMAND_REQUEUE` へマッピングし、既存の回収・エスカレーション機構を再利用。

2. **provider 境界での `launch_phase` 永続化**:
   - claim placeholder を in-memory run_state へ同期後、provider 呼出直前に該当 active entry の `launch_phase="launching"` を保存。保存失敗時は provider を呼ばず起動を保留する。
   - provider 起動成功時は local/cloud 問わず `launch_phase="launched"` を保存。
   - 明確な起動失敗時は `launch_phase="failed"` を保存。
   - `LaunchOutcomeUnknown` や例外時は `launch_phase="launching"` を維持。

3. **consistency 観測の拡張**:
   - `orchestune/consistency/vocabulary.py` に `FACT_EXECUTION_OWNER_KIND`, `FACT_EXECUTION_CLAIM_ID`, `FACT_EXECUTION_CLAIM_STAGE`, `FACT_EXECUTION_LAUNCH_PHASE` を追加。
   - `ExecutionRecord` に上記 4 フィールドを追加。
   - `dispatch/execution_repair.py` および `dispatch/cycle.py` のアダプタで `ActiveWorktree` から写す。consistency kernel が `dispatch.state` を直接 import しないレイヤー境界を維持。

4. **新 finding の判定条件**:
   - `zombie_gc_enabled is True`
   - `execution_kind == "unknown"`
   - `pid is None`, `external_id is None`, `started_at is None`
   - `worktree_exists is True`
   - `owner_kind == "dispatch"`
   - `claim_id` が非空文字列
   - `claim_stage in {"active_saved", "completed"}`
   - `launch_phase in {None, "failed"}`
   - 上記 facts がすべて `_KNOWN` であること。欠落・UNKNOWN・条件不一致時は finding なし。
   - `owner_kind == "interactive"`、`launch_phase in {"launching", "unknown", "launched"}`、pid/external_id/started_at 存在時は除外。

5. **修復直前の fresh precondition 再検証**:
   - `revalidate_reclaim_preconditions` に `_dispatch_prelaunch_orphan` を追加し、run_state とファイルシステムの最新状態を再確認。不一致時は `SKIPPED`。

## Impact scope (Serena MCP + supplementary text search)

| Reference | Decision | Rationale |
| :--- | :--- | :--- |
| `orchestune/consistency/vocabulary.py` | in scope | 新規 Fact キー（4種）および finding 名定数を定義。 |
| `orchestune/consistency/observation.py:ExecutionRecord` | in scope | `owner_kind`, `claim_id`, `claim_stage`, `launch_phase` フィールドを追加。 |
| `orchestune/consistency/observation.py:_read_*` & `_execution_observations` | in scope | 追加フィールドを読み取り Fact として emit する。 |
| `orchestune/consistency/invariants/execution.py` | in scope | `DISPATCH_PRELAUNCH_ORPHAN` の純粋判定関数を追加し、invariant 検査に登録。 |
| `orchestune/consistency/repairs/execution.py` | in scope | `DISPATCH_PRELAUNCH_ORPHAN` を `COMMAND_RECLAIM`, `COMMAND_REQUEUE` へマッピング。 |
| `orchestune/dispatch/execution_repair.py:_execution_records` | in scope | `ActiveWorktree` から `ExecutionRecord` への新規 4 フィールドマッピングを追加。 |
| `orchestune/dispatch/execution_repair.py:revalidate_reclaim_preconditions` | in scope | `_dispatch_prelaunch_orphan` helper による修復直前の fresh precondition 再検証を追加。 |
| `orchestune/dispatch/cycle.py:_DispatchConsistencyAdapter._executions` | in scope | サイクル内の `ExecutionRecord` 生成箇所に 4 フィールドマッピングを追加。 |
| `orchestune/dispatch/launch.py:_try_planned_launch` | in scope | provider 境界直前での `launching` 永続化と、失敗時の provider 呼出抑止。 |
| `orchestune/dispatch/launch.py:_apply_single_task_launch` / `_record_successful_launch` | in scope | 成功時の `launched` 永続化、明確な失敗時の `failed` 永続化。 |
| `orchestune/dispatch/gc/zombies.py` | in scope | `_build_reclaim_candidate` および `_reclaim_candidate_from_command` に `DISPATCH_PRELAUNCH_ORPHAN` を追加し、GC 回収対象に組み込み。 |
| `orchestune/claim/service.py` | still out of scope | claim の既存保存順序・契約は変更しない。 |
| `orchestune/dispatch/recovery.py` | still out of scope | durable unknown/launched attempt の復旧契約を変更しない。 |
| `tests/test_consistency_observation.py` | in scope | 新規 Fact および `ExecutionRecord` 拡張の観測テスト。 |
| `tests/test_consistency_execution_policy.py` | in scope | `DISPATCH_PRELAUNCH_ORPHAN` の生成・除外条件テスト（TDD 手順 1〜4）。 |
| `tests/test_consistency_execution_repair.py` | in scope | 修復マッピングと fresh revalidation のテスト（TDD 手順 8）。 |
| `tests/test_dispatch_launch_attempts.py` | in scope | provider 境界での `launch_phase` 永続化・結果不明時の保持テスト（TDD 手順 5〜7）。 |
| `tests/test_dispatch_gc_zombies.py` | in scope | 統合回収テストと既存 orphan テストの無変更通過確認（TDD 手順 9, 10）。 |

### Supplementary search coverage
- Dynamic access (`getattr`/`setattr`/`**kwargs`): `ActiveWorktree` および `ExecutionRecord` のフィールドアクセスを検証。動的アクセスなし。
- String-addressed test doubles: `test_consistency_*.py`, `test_dispatch_*.py` 内の mock/patch 対象を確認。
- Serialized names: `run_state.json` の `launch_phase`, `owner_kind`, `claim_id`, `claim_stage` のキー名と既存 `ActiveWorktree` シリアライズ/デシリアライズとの整合性を確認。
- Documentation/skills: レイヤー境界（consistency から dispatch.state への非依存）を確認。

## TDD Plan

1. **Step 1: Invariants & Policy テスト (TDD 1, 2, 3, 4)**
   - `test_consistency_observation.py`: `ExecutionRecord` と Fact emit のテスト追加。
   - `test_consistency_execution_policy.py`:
     - worktree あり、dispatch owner、completed claim、handle なし、launch_phase=None で `DISPATCH_PRELAUNCH_ORPHAN` が出るテスト追加。
     - interactive owner で finding が出ないテスト追加。
     - launching, unknown, launched の各 phase で finding が出ないテスト追加。
     - owner_kind, claim_id, claim_stage, launch_phase が UNKNOWN / 欠落時に自動修復しないテスト追加。
2. **Step 2: Repairs & Preconditions テスト (TDD 8)**
   - `test_consistency_execution_repair.py`:
     - `DISPATCH_PRELAUNCH_ORPHAN` が `COMMAND_RECLAIM`, `COMMAND_REQUEUE` へマップされるテスト。
     - fresh revalidation: finding 作成後に active が `launched` 等へ変わった場合、回収を SKIP するテスト。
3. **Step 3: Dispatch Launch 永続化テスト (TDD 5, 6, 7)**
   - `test_dispatch_launch_attempts.py`:
     - provider 呼出直前に `launching` が保存され、保存失敗時は provider が呼ばれないテスト。
     - 明確な起動失敗で `failed` が保存されるテスト。
     - `LaunchOutcomeUnknown` では `launching` が残り、worktree と active entry が保持されるテスト。
4. **Step 4: 統合回収 & 既存回帰テスト (TDD 9, 10)**
   - `test_dispatch_gc_zombies.py`:
     - 回収成功時に worktree 削除、queue 復帰、active 解放が既存回数制御を通る統合テスト。
     - 既存 `HANDLELESS_EXECUTION_ORPHAN`、dead local process、timeout、interactive 除外テストが無変更で通ることを確認。
5. **Step 5: ローカル CI 検証**
   - `./scripts/local-ci.sh` の実行とエラーゼロ確認。

## #822 observation record

- Start date: 2026-09-22
- Environment: Linux, Python 3.13.15, uv 0.12.10, ruff 0.4.10, mypy 1.20.2
- Base SHA: `d860f56`
- Tool: Serena MCP (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`)
- Actual use: Enumerate `ExecutionRecord`, `HANDLELESS_EXECUTION_ORPHAN`, `revalidate_reclaim_preconditions`, `launch_phase` references; supplemented by `git grep`.
- Tokens: unavailable (no counter exposed).
- Scope snapshot permalink: https://github.com/Saltmu/orchestune/issues/964#issuecomment-5769389411
