---
name: "orchestune-readiness"
description: "Pre-flight diagnosis skill verifying discipline documents and CI configuration before task decomposition or parallel execution."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Readiness Skill

This skill diagnoses repository readiness (discipline documents, CI configuration, and local CI entrypoints) before initiating task decomposition or parallel development. It separates deterministic mechanical checks (`orchestune doctor`) from semantic LLM review of document and workflow contents.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, suggestions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

## Overview & Trigger Conditions

Run this skill before starting task decomposition (`skills/orchestune/SKILL.md` Stage 1) or during replan if not already executed in the current session.

The skill runs a lightweight helper script to load configuration and execute mechanical diagnostics, then semantically inspects discipline and CI contents.

## Step 1: Mechanical Pre-Check

Execute the helper script within the Python environment where Orchestune is installed:

```bash
python <path-to-skill>/scripts/check_readiness.py --repo-root <repo-root> --execution-mode <local|actions> [--workflow <workflow-path>...]
```

Parse the JSON output:
- **`skipped: true`**: Configuration `skip-readiness-check = true` is set. Bypass readiness diagnostics immediately and proceed to decomposition. Standalone `orchestune doctor` and dispatch preflight checks remain unaffected.
- **Exit code 2**: Configuration error, invalid TOML, or invalid arguments. Report the error with exit code 2 and stop. Never treat configuration errors as skipped or passed.
- **`skipped: false`**: Read the `doctor` diagnostic report. Inspect any diagnostic entries with `warning` or `error` status (e.g. `dispatch.readiness.discipline`, `dispatch.readiness.ci_definition`, `dispatch.readiness.ci_command`).

## Step 2: Semantic Review of Discipline Documents

Inspect discipline files identified in the repository (`AGENTS.md`, `CLAUDE.md`, `.agents/AGENTS.md`, `CONTRIBUTING.md`):

1. **No discipline doc (no rules)**:
   No discipline file exists, or the file is empty without rules.
   *Remediation*: Recommend running `orchestune skills install --target all --scope project --with-workflow-skill` to install the workflow template, or creating `AGENTS.md` with standard instructions.
2. **Referenced discipline doc (referenced rules)**:
   The top-level file (e.g. `AGENTS.md`) delegates instructions to another file (e.g. `.agents/AGENTS.md`). Follow the reference, verify the target file exists, and inspect that substantive rules are defined.
3. **Circular reference**:
   File A refers to File B, which refers back to File A without terminating rules.
   *Remediation*: Point out the loop and recommend consolidating rules into one canonical source.
4. **Root-escaping reference**:
   A reference points outside the repository root (e.g. `../external/rules.md`).
   *Remediation*: Prohibit root-escaping paths; copy required instructions into repository-local files.

## Step 3: Semantic Review of CI Configuration & Entrypoints

Inspect CI workflow definitions (`.github/workflows/*.yml`) and local CI scripts:

1. **Dispatch-only workflow (no validation commands)**:
   A workflow triggers only on dispatch or label changes without running tests, linter, or builds. Flag that CI lacks validation commands.
2. **CI delegates to test script (indirect validation)**:
   A workflow runs a script (e.g. `./scripts/local-ci.sh`, `pytest`, `make ci`). Verify that the referenced script exists, is executable, and contains test/lint execution commands.
3. **Untraceable entrypoint (opaque external logic)**:
   The workflow references dynamic or remote composite actions whose commands cannot be inspected locally. Note as unverified without overwriting doctor warnings.

## Step 4: Presentation of Diagnosis

Present the evaluation to the user in their preferred language:
- Clear status summary (Pass, Warning, or Insufficient) with distinct evidence.
- Concrete suggestions with actionable remediation steps.
- Distinguish file existence from content evaluation.
