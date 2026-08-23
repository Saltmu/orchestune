import ntpath
import os
import shutil
import sys
from pathlib import Path

SKILLS_EXCLUDED_FROM_SETUP = {"local-ci-developer", "workflow-template"}
WORKFLOW_TEMPLATE_SKILL_NAME = "workflow-template"


def _normalized_path(path: str) -> str:
    """Normalize Windows extended-length paths before comparing link targets."""
    if sys.platform == "win32":
        if path.startswith("\\\\?\\UNC\\"):
            path = "\\\\" + path[8:]
        elif path.startswith("\\\\?\\"):
            path = path[4:]
        return ntpath.normcase(ntpath.normpath(path))
    return os.path.normcase(os.path.normpath(path))


def _create_skill_link(src_skill: Path, dest_skill: Path) -> str:
    """Create a skill link, falling back to a copy on unprivileged Windows."""
    try:
        dest_skill.symlink_to(src_skill, target_is_directory=True)
        return "linked"
    except OSError as e:
        if sys.platform != "win32" or getattr(e, "winerror", None) != 1314:
            raise

    shutil.copytree(src_skill, dest_skill)
    return "copied"


def _package_relative_skills_dirs() -> list[Path]:
    """Candidate `skills/` directories relative to the installed package.

    Tried, in order, as a fallback when no ancestor of `cwd` has a usable
    project-local `skills/` tree.
    """
    pkg_dir = Path(__file__).resolve().parent
    return [pkg_dir / "skills", pkg_dir.parent / "skills"]


def get_skills_source_dir() -> Path:
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        skills_dir = parent / "skills"
        if skills_dir.is_dir() and (skills_dir / "orchestune").is_dir():
            return skills_dir

    for candidate in _package_relative_skills_dirs():
        if candidate.is_dir() and (candidate / "orchestune").is_dir():
            return candidate

    raise FileNotFoundError(
        "Could not locate the 'skills' directory. "
        "Please run this command from the repository root, or ensure the package is correctly installed."
    )


def _handle_existing_skill_link(
    src_skill: Path, dest_skill: Path, skill_name: str
) -> bool | None:
    if not (dest_skill.exists() or dest_skill.is_symlink()):
        return None

    if not dest_skill.is_symlink():
        if dest_skill.is_dir() and (dest_skill / "SKILL.md").is_file():
            print(
                f"  Skipped '{skill_name}' (already present as a copy at {dest_skill})"
            )
            return True
        print(
            f"  Error: Path '{dest_skill}' is occupied by an unrelated file/directory "
            "and does not look like a valid skill installation.",
            file=sys.stderr,
        )
        return False

    try:
        link_target = dest_skill.readlink()
        if _normalized_path(str(link_target.resolve())) == _normalized_path(
            str(src_skill.resolve())
        ):
            print(f"  Skipped '{skill_name}' (already correctly linked to {src_skill})")
            return True
        print(
            f"  Updating link for '{skill_name}' (points to {link_target} -> updating to {src_skill})"
        )
        dest_skill.unlink()
    except Exception as e:
        print(
            f"  Warning: Failed to resolve existing link {dest_skill}: {e}. Trying to overwrite."
        )
        try:
            dest_skill.unlink()
        except Exception as unlink_e:
            print(
                f"  Error: Failed to remove stale link {dest_skill}: {unlink_e}",
                file=sys.stderr,
            )
            return False
    return None


def _link_one_skill(src_skill: Path, dest_skill: Path, skill_name: str) -> bool:
    """Link (or copy) a single skill into place. Returns True on success/already-ok."""
    handled = _handle_existing_skill_link(src_skill, dest_skill, skill_name)
    if handled is not None:
        return handled

    try:
        setup_method = _create_skill_link(src_skill, dest_skill)
        if setup_method == "copied":
            print(
                f"  Successfully copied '{skill_name}' to {dest_skill} "
                "(symlink privilege is not available)"
            )
        else:
            print(f"  Successfully linked '{skill_name}' to {dest_skill}")
        return True
    except Exception as e:
        print(f"  Error: Failed to set up '{skill_name}': {e}", file=sys.stderr)
        return False


def _setup_for_assistant(
    assistant_name: str,
    base_dir: Path,
    target_dir: Path,
    skills_dir: Path,
    skills_to_link: list[str],
) -> tuple[int, int] | None:
    """Set up all skills for one assistant. Returns (success, failure) counts, or
    None if the assistant's base directory was not found (not applicable)."""
    if not base_dir.is_dir():
        print(f"Skipping {assistant_name} (base directory {base_dir} not found).")
        return None

    print(f"Setting up skills for {assistant_name}...")

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"  Warning: Could not create directory {target_dir}: {e}")
        return (0, max(len(skills_to_link), 1))

    success_count = 0
    failure_count = 0
    for skill_name in skills_to_link:
        src_skill = skills_dir / skill_name
        if not src_skill.is_dir():
            print(
                f"  Warning: Skill source '{skill_name}' not found in {skills_dir}, skipping."
            )
            continue

        dest_skill = target_dir / skill_name
        if _link_one_skill(src_skill, dest_skill, skill_name):
            success_count += 1
        else:
            failure_count += 1

    return (success_count, failure_count)


def _copy_workflow_template_skill(
    src_skill: Path, dest_skill: Path, skill_name: str
) -> bool:
    """`workflow-template`スキルをプロジェクトローカルへ実体コピーする。

    シンボリックリンクではなく実体コピーにする: コピー元はOrchestuneパッケージ内
    にしか存在せず、対象プロジェクト内には存在しないため（`_link_one_skill`が
    前提とする「同一マシン上のOrchestuneソースを指すリンク」が成立しない）。
    Returns True on success/already-ok.
    """
    if dest_skill.exists():
        if (dest_skill / "SKILL.md").is_file():
            print(f"  Skipped '{skill_name}' (already present at {dest_skill})")
            return True
        print(
            f"  Error: Path '{dest_skill}' is occupied by an unrelated file/directory "
            "and does not look like a valid skill installation.",
            file=sys.stderr,
        )
        return False

    try:
        dest_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_skill, dest_skill)
        print(f"  Successfully copied '{skill_name}' to {dest_skill}")
        return True
    except Exception as e:
        print(
            f"  Error: Failed to copy '{skill_name}' to {dest_skill}: {e}",
            file=sys.stderr,
        )
        return False


def _setup_project_workflow_skill(
    skills_dir: Path, home: Path
) -> tuple[bool, int, int]:
    src_workflow_skill = skills_dir / WORKFLOW_TEMPLATE_SKILL_NAME
    if not src_workflow_skill.is_dir():
        fallback = next(
            (
                candidate / WORKFLOW_TEMPLATE_SKILL_NAME
                for candidate in _package_relative_skills_dirs()
                if (candidate / WORKFLOW_TEMPLATE_SKILL_NAME).is_dir()
            ),
            None,
        )
        if fallback is not None:
            src_workflow_skill = fallback

    if not src_workflow_skill.is_dir():
        print(
            f"  Error: workflow template skill source not found in {skills_dir}, "
            "cannot fulfill --with-workflow-skill.",
            file=sys.stderr,
        )
        return False, 0, 1

    project_dir = Path.cwd()
    project_skill_dirs = {
        "Claude Code": (home / ".claude", project_dir / ".claude" / "skills"),
        "Codex CLI": (home / ".codex", project_dir / ".codex" / "skills"),
        "Antigravity": (
            home / ".gemini",
            project_dir / ".gemini" / "config" / "skills",
        ),
    }
    assistants_detected = False
    success_count = 0
    failure_count = 0
    for base_dir, project_skills_dir in project_skill_dirs.values():
        if not base_dir.is_dir():
            continue
        assistants_detected = True
        dest_skill = project_skills_dir / WORKFLOW_TEMPLATE_SKILL_NAME
        if _copy_workflow_template_skill(
            src_workflow_skill, dest_skill, WORKFLOW_TEMPLATE_SKILL_NAME
        ):
            success_count += 1
        else:
            failure_count += 1
    return assistants_detected, success_count, failure_count


def _evaluate_setup_outcome(
    assistants_detected: bool, success_count: int, failure_count: int
) -> int:
    if not assistants_detected and failure_count == 0:
        print(
            "\nNo supported AI assistants (Claude Code, Codex CLI, Antigravity) detected in your home directory."
        )
        print("Please ensure at least one assistant is installed before running setup.")
        return 0

    if failure_count > 0 and success_count == 0:
        print(
            f"\nSetup failed: all {failure_count} skill link(s) could not be created or verified.",
            file=sys.stderr,
        )
        return 1

    if failure_count > 0:
        print(
            f"\nSetup completed with {failure_count} failure(s) "
            f"({success_count} succeeded, {failure_count} failed)."
        )
        return 1

    print("\nSetup completed.")
    return 0


def setup_skills(with_workflow_skill: bool = False) -> int:
    """Set up skill links for detected AI assistants.

    `with_workflow_skill=True`は、`WORKFLOW_TEMPLATE_SKILL_NAME`（#394）を
    グローバルスキルディレクトリではなく、カレントディレクトリ（対象
    プロジェクトのルート）を基点にしたプロジェクトローカルなスキル
    ディレクトリへ実体コピーする。

    Returns an exit code: 0 on full success (or nothing to do), 1 if any
    required skill link could not be created/verified.
    """
    try:
        skills_dir = get_skills_source_dir()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    home = Path.home()
    targets = {
        "Claude Code": (home / ".claude", home / ".claude" / "skills"),
        "Codex CLI": (home / ".codex", home / ".codex" / "skills"),
        "Antigravity": (home / ".gemini", home / ".gemini" / "config" / "skills"),
    }
    skills_to_link = sorted(
        [
            d.name
            for d in skills_dir.iterdir()
            if (
                d.is_dir()
                and (d / "SKILL.md").is_file()
                and d.name not in SKILLS_EXCLUDED_FROM_SETUP
            )
        ]
    )
    assistants_detected = False
    success_count = 0
    failure_count = 0

    for assistant_name, (base_dir, target_dir) in targets.items():
        result = _setup_for_assistant(
            assistant_name, base_dir, target_dir, skills_dir, skills_to_link
        )
        if result is None:
            continue
        assistants_detected = True
        assistant_success, assistant_failure = result
        success_count += assistant_success
        failure_count += assistant_failure

    if with_workflow_skill:
        wf_detected, wf_success, wf_failure = _setup_project_workflow_skill(
            skills_dir, home
        )
        if wf_detected:
            assistants_detected = True
        success_count += wf_success
        failure_count += wf_failure

    return _evaluate_setup_outcome(assistants_detected, success_count, failure_count)
