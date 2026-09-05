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
