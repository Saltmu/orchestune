from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

from orchestune.forge import (
    REQUIRED_LABELS,
    BootstrapResult,
    ForgeError,
    GitHubForge,
    LabelSpec,
    RepoAdminForge,
)
from orchestune.infra.cli_output import Level, make_output
from orchestune.infra.git_cli import run_git

_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class _RepositoryLabelForge(Protocol):
    def read_effective_repository(self, *, cwd: Path | None = None) -> str: ...

    def ensure_labels(
        self,
        labels: tuple[LabelSpec, ...],
        *,
        repository: str | None = None,
    ) -> BootstrapResult: ...


def _is_git_repo_empty(cwd: Path) -> bool:
    try:
        run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

    res = run_git(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return res.returncode != 0


def _initialize_empty_repo(cwd: Path) -> None:
    res_staged = run_git(["ls-files", "--stage"], cwd=cwd, check=True)
    if res_staged.stdout.strip():
        raise RuntimeError("Staged files already exist in index before initial commit.")

    make_output(sys.stderr).message(
        Level.INFO,
        "Detected an empty Git repository. Initializing with initial commit...",
    )

    try:
        run_git(["config", "user.name"], cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        run_git(["config", "user.name", "orchestune-bootstrap"], cwd=cwd, check=True)

    try:
        run_git(["config", "user.email"], cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        run_git(
            ["config", "user.email", "orchestune-bootstrap@users.noreply.github.com"],
            cwd=cwd,
            check=True,
        )

    readme_path = cwd / "README.md"
    if not readme_path.exists():
        readme_path.write_text("# Initialized by Orchestune\n", encoding="utf-8")

    run_git(["add", "README.md"], cwd=cwd, check=True)
    run_git(["commit", "-m", "Initial commit"], cwd=cwd, check=True)

    run_git(["branch", "-M", "main"], cwd=cwd, check=True)

    res_remote = run_git(["remote"], cwd=cwd, check=True)
    if "origin" in res_remote.stdout.splitlines():
        make_output(sys.stderr).message(
            Level.INFO, "Pushing initial commit to origin/main..."
        )
        run_git(["push", "-u", "origin", "main"], cwd=cwd, check=True)


def run_bootstrap(forge: RepoAdminForge | None = None, cwd: Path = Path(".")) -> int:
    forge = forge or GitHubForge()

    if _is_git_repo_empty(cwd):
        try:
            _initialize_empty_repo(cwd)
        except subprocess.CalledProcessError as e:
            # #294レビュー対応: run_gitはcapture_output=Trueで実行するため、
            # 失敗したgitコマンドのstderrは端末に継承されず`e.stderr`に
            # 格納される。str(e)だけでは終了コードしか分からず、認証・権限
            # エラー等の実際の原因が失われるため、明示的に含める。
            detail = e.stderr.strip() if e.stderr else str(e)
            make_output(sys.stderr).message(
                Level.FAILURE, f"Error initializing empty repository: {detail}"
            )
            return 1
        except Exception as e:  # noqa: BLE001 - boundary: convert unexpected repository bootstrap failures to the existing CLI result
            make_output(sys.stderr).message(
                Level.FAILURE, f"Error initializing empty repository: {e}"
            )
            return 1

    return _apply_required_labels(forge)


def prepare_github_labels(
    forge: RepoAdminForge | None = None,
    *,
    cwd: Path = Path("."),
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """照合済みの対象を確認してから、不足する必須ラベルを作成する。"""
    forge = forge or GitHubForge()
    checkout = _checkout_github_repository(cwd)
    if checkout is None:
        return _fail("cannot resolve the checkout GitHub repository from origin")
    try:
        forge.check_auth()
        effective = _effective_repository(forge, cwd)
    except ForgeError as error:
        return _fail(str(error))
    if checkout.casefold() != effective.casefold():
        return _fail(
            f"GitHub target mismatch: checkout {checkout}, effective {effective}"
        )
    if confirm is not None and not confirm(checkout):
        return 0
    return _apply_required_labels(forge, repository=checkout)


def _apply_required_labels(
    forge: RepoAdminForge, *, repository: str | None = None
) -> int:
    try:
        if repository is None:
            forge.check_auth()
        result = _ensure_required_labels(forge, repository)
    except ForgeError as error:
        return _fail(str(error))
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if isinstance(error.stderr, str) else ""
        return _fail(detail or str(error))
    _report_labels(result)
    return 0


def _ensure_required_labels(
    forge: RepoAdminForge, repository: str | None
) -> BootstrapResult:
    if repository is None:
        return forge.ensure_labels(REQUIRED_LABELS)
    bound = cast(_RepositoryLabelForge, forge)
    return bound.ensure_labels(REQUIRED_LABELS, repository=repository)


def _effective_repository(forge: RepoAdminForge, cwd: Path) -> str:
    reader = getattr(forge, "read_effective_repository", None)
    if not callable(reader):
        raise ForgeError("GitHubの実効リポジトリを照合できません")
    try:
        slug = reader(cwd=cwd)
    except TypeError:
        slug = reader()
    if not isinstance(slug, str) or _OWNER_REPO.fullmatch(slug) is None:
        raise ForgeError("GitHubの実効リポジトリを照合できません")
    return slug


def _report_labels(result: BootstrapResult) -> None:
    stdout = make_output(sys.stdout)
    created = result.created_labels
    stdout.message(
        Level.SUCCESS if created else Level.INFO,
        f"Labels created: {len(created)}",
    )
    for name in created:
        stdout.message(Level.SUCCESS, f"created label {name}")
    stdout.message(Level.INFO, f"Labels already present: {len(result.existing_labels)}")


def _fail(message: str) -> int:
    make_output(sys.stderr).message(Level.FAILURE, f"Error: {message}")
    return 1


def _checkout_github_repository(cwd: Path) -> str | None:
    url = _origin_url(cwd)
    if url is None:
        return None
    return _owner_and_repo(url)


def _origin_url(cwd: Path) -> str | None:
    try:
        completed = run_git(["remote", "get-url", "origin"], cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _owner_and_repo(url: str) -> str | None:
    text = url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    slug = _scp_owner_and_repo(text)
    if slug is None:
        slug = _url_owner_and_repo(text)
    if slug is None or _OWNER_REPO.fullmatch(slug) is None:
        return None
    return slug


def _scp_owner_and_repo(text: str) -> str | None:
    prefix = "git@github.com:"
    if "://" in text or not text.lower().startswith(prefix):
        return None
    parts = [part for part in text[len(prefix) :].split("/") if part]
    if len(parts) != 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _url_owner_and_repo(text: str) -> str | None:
    if "://" not in text:
        return None
    parsed = urlparse(text)
    host = parsed.hostname
    if host is None or host.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="orchestune bootstrap",
        description=("Check gh authentication and create the required GitHub labels."),
    )
    parser.parse_args(argv)
    sys.exit(run_bootstrap())
