"""Resolve configured completion CI from the primary checkout."""

from pathlib import Path

from orchestune.claim.workspace import resolve_checkout_roots
from orchestune.complete.ci_evidence import CiExecutionError
from orchestune.config_contracts import ConfigError
from orchestune.config_schema import parse_ci_command, validate_toml_config
from orchestune.infra.repository_config import find_and_load_config_file


def configured_ci_command(worktree_root: Path | str | None = None) -> list[str] | None:
    """Read operational settings without changing the command's worktree cwd."""
    primary, _ = resolve_checkout_roots(
        Path(worktree_root) if worktree_root is not None else Path.cwd()
    )
    source = primary / (
        "orchestune.toml"
        if (primary / "orchestune.toml").exists()
        else "pyproject.toml"
    )
    try:
        config = validate_toml_config(find_and_load_config_file(primary))
        return (
            parse_ci_command(config["ci_command"]) if "ci_command" in config else None
        )
    except ConfigError as exc:
        raise CiExecutionError(
            f"Invalid CI configuration in {source}: {exc}. Set ci-command to a command such as 'pytest' (under [tool.orchestune] for pyproject.toml)."
        ) from exc
