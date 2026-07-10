# Contributing to Orchestune

[English](CONTRIBUTING.md) | [日本語](CONTRIBUTING.ja.md)

This document covers how to set up a local development environment for Orchestune itself. If you just want to *use* Orchestune in another project, see the [README](README.md) instead.

## Setup

Ensure you have Python 3.12+, Poetry, the GitHub CLI (`gh auth status`), and [gitleaks](https://github.com/gitleaks/gitleaks#installing) installed, then install dependencies:

```bash
poetry install
```

## Running Tests

Execute unit tests and coverage checks using `pytest`:
```bash
poetry run pytest
```

## Local CI Script

Before committing or pushing your changes, run the local CI script to verify formatting, types, and tests:
```bash
./scripts/local-ci.sh
```
This runs:
1. **Ruff Format & Lint Check**: `ruff format` and `ruff check`
2. **Mypy Type Check**: Type hint validation
3. **Pytest Coverage Check**: Ensures coverage does not drop below 75%
4. **Secret & Local Path Scan** (`gitleaks`): Blocks commits/pushes that leak secrets or absolute local paths (e.g. `file:///home/<user>/...`). Config lives in [`.gitleaks.toml`](.gitleaks.toml). Also enforced in CI regardless of local setup.
