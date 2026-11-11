# Orchestune

**Detect conflicts between parallel AI coding agents before they write code, and keep every task and result traceable in GitHub Issues.**

[English](README.md) | [日本語](README.ja.md)

Orchestune turns an approved decomposition plan into dependency-aware GitHub Issues, coordinates agents in isolated worktrees, and links their branches, pull requests, and CI results back to the original task. It runs as a skill for AI coding assistants such as Claude Code, Codex CLI, and Antigravity.

## Is it for you?

Use Orchestune for features spanning multiple modules, changes with shared interfaces or dependencies, or development of the agent skills themselves. It suits individual developers and small teams who want substantial work to continue while they are away.

For a simple web app or a small task that fits in one prompt, asking an agent directly is faster and uses less quota. Decomposition and dispatch become useful when coordinating the work is itself a problem.

## One real development run

In Orchestune itself, [parent Issue #1368](https://github.com/Saltmu/orchestune/issues/1368) split the Rich CLI rollout into **8 child tasks**: shared output primitives, status/doctor, dispatch progress, provision, lifecycle commands, setup commands, DAG/recovery/scratch, and root help/README.

From the creation of the [first implementation PR #1419](https://github.com/Saltmu/orchestune/pull/1419) to the merge of the [final integration PR #1439](https://github.com/Saltmu/orchestune/pull/1439), the run spanned **about 12 hours 41 minutes** (2026-10-25 11:21 to 2026-10-26 00:02 UTC). This is elapsed time, including waits, rather than agent execution time.

The normal workflow has two human decision points: approve the decomposition and merge the final PR. This run also needed [operator recovery during integration](https://github.com/Saltmu/orchestune/issues/1368#issuecomment-6398337132).

## Try your first plan

With Python 3.12+, uv, and an authenticated GitHub CLI available, run these commands in the repository you want to work on:

```bash
uv tool install "git+https://github.com/Saltmu/orchestune.git"
orchestune skills install --target all --scope project
orchestune config init
```

Ask your coding assistant: “Use orchestune to decompose this feature and tell me the saved plan path.” Then validate it before approving Issue creation or dispatch, replacing the example path below with the path returned by the assistant:

```bash
orchestune dag --plan "<saved-plan-path>"
```

### Local Trial (No GitHub Setup Required)

A local trial needs Orchestune, Git, a supported Python available as `python`, and an authenticated Codex CLI or Claude Code on `PATH`. Model calls stay online. GitHub authentication, `gh`, a remote, labels, and dispatch settings are not part of the trial. Those belong to GitHub onboarding for an existing project.

The trial confirms that you can approve a sample plan, read the diff after CI and a separate review session pass on the same head, and choose whether to keep or adopt it.

Use the same-language quickstart in the [Setup Guide](docs/en/setup.md#local-trial-setup). Command details are also in the [Usage Reference](docs/en/usage.md#10-local-autonomous-trial-orchestune-trial).

For an existing project, check the GitHub onboarding prerequisites in the [Setup Guide](docs/en/setup.md) before the commands above. See [Installation](#installation) below for other installation options and agent configuration.

⚠️ **Beta**: The core workflow is operational, but APIs, CLI commands, and workflows may change before the stable release.

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

DAG validation, self-healing state recovery from GitHub, and the two-gate human approval model (see [Architecture & Design](docs/en/architecture.md)) all exist for one reason: so the pipeline holds together when nobody is watching.

The pipeline itself is advanced by deterministic Python. An LLM call is a scarce operation that consumes quota, so it is spent only where judgment cannot be replaced — and the automated shared-state transitions are Python's (see [2.2 Determinism](docs/en/architecture.md#determinism)).

---

## Installation

👉 Before adopting Orchestune, check the prerequisites your target repository must satisfy (agent discipline definition, CI thoroughness, `ci_command` setting) in [Setup Guide § 0. Prerequisites](docs/en/setup.md#0-prerequisites).

Ensure you have Python 3.12+, uv, and the GitHub CLI installed.

```bash
# Install CLI globally using uv tool (recommended) or pipx
uv tool install "orchestune==<RELEASE_VERSION>"
# Or install latest from git:
uv tool install "git+https://github.com/Saltmu/orchestune.git"
```

After installation, install Orchestune skills to your AI assistants (Claude Code, Codex CLI, Antigravity IDE, Antigravity CLI, Grok CLI):

```bash
# Install to current project (recommended for team sharing):
orchestune skills install --target all --scope project

# Or install to user configuration directory globally:
orchestune skills install --target all --scope user
```

👉 For diagnostics (`orchestune skills doctor`), update/uninstall, or Cloud Routine configuration, see the [Setup Guide](docs/en/setup.md).

---

## Usage

In practice, you don't type the commands below yourself. You describe the task to your AI agent in natural language (e.g. "decompose this feature with orchestune"), and the `orchestune` skill drives the whole pipeline internally — including handing off to `orchestune-provision` or `orchestune-dispatch` — calling these CLI commands as tool calls on your behalf. They're shown here to make each stage concrete, and because you may still want to run a step manually in exceptional cases (e.g. resuming dispatch after local state is lost).

1. **Decompose and validate**: Your agent turns the large task into a reviewable `decomposition_plan.md`, then validates its dependency DAG and conflict risks — you review and approve the plan.
2. **Provision**: Once you approve, your agent creates dependency-linked parent and child GitHub Issues from the plan.
3. **Dispatch**: Your agent starts eligible subtasks in isolated worktrees using local or cloud coding agents.
4. **Trace and integrate**: Each subtask is tracked through its Issue, branch, pull request, and CI result while Orchestune coordinates dependent work and integration.

```bash
# 1. Create or edit configuration (interactive wizard)
orchestune config init
# Edit existing configuration: orchestune config edit

# 2. Validate the decomposition plan's DAG
orchestune dag --plan decomposition_plan.md

# 3. Preview, then create, the GitHub Issues from the approved plan
orchestune provision --plan decomposition_plan.md
orchestune provision --plan decomposition_plan.md --apply

# 4. Start the dispatcher (dry-run, then execute)
orchestune dispatch
orchestune dispatch --apply
```

👉 For CLI options and `decomposition_plan.md` syntax specification, see the [Usage & Command Reference](docs/en/usage.md).

---

## CLI output

Orchestune uses Rich formatting for human-facing output in an interactive terminal. It selects plain text automatically for piped or redirected output, CI, and `TERM=dumb`. Set `ORCHESTUNE_CLI_PLAIN=1` to force plain output in any environment.

Set `NO_COLOR` to any non-empty value to disable color. On a TTY, tables and live progress can still be used without color.

Existing machine-readable output stays suitable for scripts: JSON responses and the path printed by `orchestune scratch create` are not decorated or mixed with progress text. `orchestune status --watch` updates in place on a TTY; in plain or non-TTY mode it appends a static snapshot at each watch interval without clearing the screen.

## Contributing

Want to develop Orchestune itself (run its test suite, local CI, etc.)? See [CONTRIBUTING.md](CONTRIBUTING.md).
