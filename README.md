# Orchestune

[English](README.md) | [日本語](README.ja.md)

Orchestune is a multi-agent implementation orchestrator designed to coordinate parallel development tasks. It automates DAG construction, scheduling, dispatch cycles, self-healing, and pull request integration.

Orchestune is provided as a **Skill for Agentic AI development** (e.g., Claude Code, Antigravity), allowing an AI agent to autonomously decompose tasks, dispatch subtasks, and integrate results.

## Key Features

1. **DAG Construction & Conflict Prevention**
   - Statically computes dependencies based on file and symbol overlap similarity metrics, building conflict-free DAGs for safe parallel execution.
2. **Intelligent Dispatch & Scheduling**
   - Supports local command execution as well as dispatching agents via Claude Code Cloud Routine.
3. **Self-healing State Recovery**
   - Optimized for stateless CI/CD environments (like GitHub Actions); automatically reconstructs state using active GitHub Issues and PRs.
4. **Integration & Rebase Coordination**
   - Monitors completed subtask PRs, orchestrating merges and rebasing downstream branches automatically to minimize conflicts.

👉 For more details about the design, see [Architecture & Design](docs/en/architecture.md).

---

## Installation

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

A typical Orchestune workflow follows these steps:

1. **Decompose**: Ask your AI agent to decompose a feature using `orchestune`, which drafts and validates a `decomposition_plan.md`.
2. **Dispatch**: Once the plan is approved, run the dispatcher to spin up worktrees and start subtask agents.

```bash
# Validate your decomposition plan's DAG
orchestune dag --plan decomposition_plan.md

# Run dispatcher in dry-run mode
orchestune dispatch --no-apply

# Run dispatcher (execute)
orchestune dispatch
```

👉 For CLI options and `decomposition_plan.md` syntax specification, see the [Usage & Command Reference](docs/en/usage.md).

---

## Contributing

Want to develop Orchestune itself (run its test suite, local CI, etc.)? See [CONTRIBUTING.md](CONTRIBUTING.md).
