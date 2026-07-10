import sys
from pathlib import Path


def get_skills_source_dir() -> Path:
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        skills_dir = parent / "skills"
        if skills_dir.is_dir() and (skills_dir / "orchestune").is_dir():
            return skills_dir

    pkg_dir = Path(__file__).resolve().parent
    pkg_skills_dir = pkg_dir / "skills"
    if pkg_skills_dir.is_dir() and (pkg_skills_dir / "orchestune").is_dir():
        return pkg_skills_dir

    parent_skills_dir = pkg_dir.parent / "skills"
    if parent_skills_dir.is_dir() and (parent_skills_dir / "orchestune").is_dir():
        return parent_skills_dir

    raise FileNotFoundError(
        "Could not locate the 'skills' directory. "
        "Please run this command from the repository root, or ensure the package is correctly installed."
    )


def setup_skills() -> None:
    try:
        skills_dir = get_skills_source_dir()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    home = Path.home()

    targets = {
        "Claude Code": (home / ".claude", home / ".claude" / "skills"),
        "Codex CLI": (home / ".codex", home / ".codex" / "skills"),
        "Antigravity": (home / ".gemini", home / ".gemini" / "config" / "skills"),
    }

    skills_to_link = ["orchestune", "orchestune-dispatch", "local-ci-developer"]
    setup_any = False

    for assistant_name, (base_dir, target_dir) in targets.items():
        if not base_dir.is_dir():
            print(f"Skipping {assistant_name} (base directory {base_dir} not found).")
            continue

        print(f"Setting up skills for {assistant_name}...")
        setup_any = True

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"  Warning: Could not create directory {target_dir}: {e}")
            continue

        for skill_name in skills_to_link:
            src_skill = skills_dir / skill_name
            if not src_skill.is_dir():
                print(
                    f"  Warning: Skill source '{skill_name}' not found in {skills_dir}, skipping."
                )
                continue

            dest_skill = target_dir / skill_name

            if dest_skill.exists() or dest_skill.is_symlink():
                if dest_skill.is_symlink():
                    try:
                        link_target = dest_skill.readlink()
                        if link_target.resolve() == src_skill.resolve():
                            print(
                                f"  Skipped '{skill_name}' (already correctly linked to {src_skill})"
                            )
                            continue
                        else:
                            print(
                                f"  Updating link for '{skill_name}' (points to {link_target} -> updating to {src_skill})"
                            )
                            dest_skill.unlink()
                    except Exception as e:
                        print(
                            f"  Warning: Failed to resolve existing link {dest_skill}: {e}. Trying to overwrite."
                        )
                        dest_skill.unlink()
                else:
                    print(
                        f"  Skipped '{skill_name}' (a directory/file already exists at {dest_skill})"
                    )
                    continue

            try:
                dest_skill.symlink_to(src_skill, target_is_directory=True)
                print(f"  Successfully linked '{skill_name}' to {dest_skill}")
            except Exception as e:
                print(
                    f"  Error: Failed to create symlink for '{skill_name}': {e}",
                    file=sys.stderr,
                )

    if not setup_any:
        print(
            "\nNo supported AI assistants (Claude Code, Codex CLI, Antigravity) detected in your home directory."
        )
        print("Please ensure at least one assistant is installed before running setup.")
    else:
        print("\nSetup completed.")
