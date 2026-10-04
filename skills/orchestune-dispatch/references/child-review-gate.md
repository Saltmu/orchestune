# Child Review-Evidence Gate Reference

Before the Integrator updates `parent/issue-{N}` during scheduled dispatch cycles, it verifies that every child sub-issue in the merge has passing review evidence in its canonical Outcome Record. This ensures that only reviewed and verified tasks are integrated automatically.

## Responsibility Split
* **Child Agent / Development Skill (Step 11)**: Runs automated review on the child PR and records an LLM judgment (`adopt`, `decline`, etc.) for every finding.
* **Child Completion (`orchestune complete`)**: Checks that the judgment table covers every current finding and matches HEAD SHA, saving review evidence in the done Outcome Record.
* **Integrator**: Verifies the saved evidence before merging into the parent branch; never runs a review itself.

## Pass Condition
The child Issue's latest Outcome Record must have `result=done` and `verdict=pass`, and both `head_sha` and `reviewed_head_sha` must equal the commit SHA about to be merged.

## Gate Failure Reasons
| Reason | Meaning | How to resume |
| :--- | :--- | :--- |
| `legacy` | Outcome recorded before gate existed | Review on PR and rerun complete, or use gate off |
| `skipped` | Review was explicitly skipped | Rerun review and complete, or accept gate off |
| `not_pass` | `verdict` is not `pass` | Resolve findings so review passes, then rerun complete |
| `sha_mismatch` | Child HEAD moved after `complete` | Review new HEAD and rerun complete |
| `absent` | Child Issue has no Outcome Record | Run `orchestune complete` for the child |
| `lookup_unknown` | Reading child comments failed | Re-run dispatch; no action needed on child |
| `integration_evidence_missing` | Missing integration proof | Restore evidence and re-run dispatch |

## Configuration & Recovery
* Setting: `--child-review-gate {required,off}` (CLI option) or `child-review-gate = "required"|"off"` in `orchestune.toml`.
* When the gate stops integration: the parent branch is not updated, and the parent Issue moves to `status:blocked-human-review`.
* Turning `--child-review-gate off` is an emergency recovery option that bypasses review checks for all children in that run. Only use it with explicit human approval.
