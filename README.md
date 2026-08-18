# Orchestune

[English](README.md) | [日本語](README.ja.md)

⚠️ **Beta**: Orchestune's core workflow is operational, but APIs, CLI commands, and workflows may still change before the stable release.

**Orchestune lets you delegate large development tasks to AI agents while keeping every subtask, dependency, and result traceable.**

It turns an approved decomposition plan into dependency-aware GitHub Issues, coordinates autonomous implementation by multiple agents, and links the resulting branches, pull requests, and CI outcomes back to the original task. Orchestune is provided as a **Skill for Agentic AI development** (e.g., Claude Code, Codex CLI, and Antigravity).

## Key Features

1. **Decompose large tasks into traceable work units**
   - Creates a reviewable plan in which every subtask has an explicit scope, acceptance criteria, verification steps, dependencies, and expected file or symbol footprint.
2. **Validate dependencies and conflicts before implementation**
   - Builds a DAG from the plan and detects dependency cycles, overlapping files or symbols, shared-contract risks, and unsafe parallel work before agents modify the codebase.
3. **Provision an auditable execution plan on GitHub**
   - Converts the approved plan into parent and child Issues with dependency links, status labels, and implementation context, preserving why each unit of work exists and how it should be verified.
4. **Coordinate autonomous implementation and integration**
   - Dispatches ready Issues to local or cloud coding agents, reconstructs progress from GitHub Issues and PRs, and coordinates downstream rebases and integration while keeping the full development history reviewable.

👉 For more details about the design, see [Architecture & Design](docs/en/architecture.md).
👉 For the meaning and lifecycle of each `status:*` GitHub label, see [status:* Label Lifecycle](docs/en/status-labels.md).

---

## Design Philosophy: Quota Efficiency

Orchestune is for individual developers and small teams working with AI coding agents. The scarce resource here is not wall-clock time — it is **your AI usage quota (a subscription's session/weekly allowance) and the hours a human can actually be at the desk.**

For a small task, asking an agent directly is faster and cheaper than going through decomposition and dispatch. Orchestune pays for itself only on tasks large enough that you want them to keep running while you are away — overnight, or on a stateless CI runner.

DAG validation, self-healing state recovery from GitHub, and the two-gate human approval model (see [Architecture & Design](docs/en/architecture.md)) all exist for one reason: so the pipeline holds together when nobody is watching.

The pipeline itself is advanced by deterministic Python. An LLM call is a scarce operation that consumes quota, so it is spent only where judgment cannot be replaced — and state transitions are, as a rule, Python's (see [0.1 Determinism](docs/en/architecture.md#01-determinism-the-llm-only-judges-python-owns-every-state-transition)).

---

## Installation

👉 Before adopting Orchestune, check the prerequisites your target repository must satisfy (agent discipline definition, CI thoroughness, `ci_command` setting) in [Setup Guide § 0. Prerequisites](docs/en/setup.md#0-prerequisites).

Ensure you have Python 3.12+, Poetry, and the GitHub CLI installed.

```bash
# Install globally using pipx (recommended)
pipx install git+https://github.com/Saltmu/orchestune.git
```

After installation, run the following setup command to automatically link Orchestune skills to your AI assistants (Claude Code, Codex CLI, Antigravity):

```bash
orchestune setup
```

👉 For adding Orchestune as a development dependency, manual skill setup, or Cloud Routine configuration, see the [Setup Guide](docs/en/setup.md).

---

## Usage

In practice, you don't type the commands below yourself. You describe the task to your AI agent in natural language (e.g. "decompose this feature with orchestune"), and the `orchestune` skill drives the whole pipeline internally — including handing off to `orchestune-provision` or `orchestune-dispatch` — calling these CLI commands as tool calls on your behalf. They're shown here to make each stage concrete, and because you may still want to run a step manually in exceptional cases (e.g. resuming dispatch after local state is lost).

1. **Decompose and validate**: Your agent turns the large task into a reviewable `decomposition_plan.md`, then validates its dependency DAG and conflict risks — you review and approve the plan.
2. **Provision**: Once you approve, your agent creates dependency-linked parent and child GitHub Issues from the plan.
3. **Dispatch**: Your agent starts eligible subtasks in isolated worktrees using local or cloud coding agents.
4. **Trace and integrate**: Each subtask is tracked through its Issue, branch, pull request, and CI result while Orchestune coordinates dependent work and integration.

```bash
# What the orchestune skill runs on your behalf at each stage:

# 1. Validate the decomposition plan's DAG
orchestune dag --plan decomposition_plan.md

# 2. Preview, then create, the GitHub Issues from the approved plan
orchestune provision --plan decomposition_plan.md --no-apply
orchestune provision --plan decomposition_plan.md

# 3. Start the dispatcher (dry-run, then execute)
orchestune dispatch --no-apply
orchestune dispatch
```

👉 For CLI options and `decomposition_plan.md` syntax specification, see the [Usage & Command Reference](docs/en/usage.md).

---

## Contributing

Want to develop Orchestune itself (run its test suite, local CI, etc.)? See [CONTRIBUTING.md](CONTRIBUTING.md).
