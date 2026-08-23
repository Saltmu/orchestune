from pathlib import Path
from unittest.mock import patch

from orchestune.setup_skills import setup_skills


def test_setup_skills_without_flag_does_not_copy_workflow_template(tmp_path):
    """#394: `--with-workflow-skill`を指定しない限り、workflow-templateは
    プロジェクトローカルへコピーされない（後方互換）。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    (skills_dir / "workflow-template").mkdir()
    (skills_dir / "workflow-template" / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills()

    assert not (mock_source / ".claude" / "skills" / "workflow-template").exists()
    # workflow-templateはグローバル自動リンクの対象からも除外される
    assert not (mock_home / ".claude" / "skills" / "workflow-template").exists()


def test_setup_skills_with_workflow_skill_copies_project_local(tmp_path):
    """#394: `--with-workflow-skill`指定時、検出済みアシスタントそれぞれの
    プロジェクトローカルなスキルディレクトリへworkflow-templateが実体コピー
    される（シンボリックリンクではない: コピー元は対象プロジェクト内には
    存在しないため）。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()
    (mock_home / ".codex").mkdir()
    (mock_home / ".gemini").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    (skills_dir / "workflow-template").mkdir()
    (skills_dir / "workflow-template" / "SKILL.md").write_text(
        "template contents", encoding="utf-8"
    )

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    assert exit_code == 0
    claude_target = mock_source / ".claude" / "skills" / "workflow-template"
    codex_target = mock_source / ".codex" / "skills" / "workflow-template"
    gemini_target = mock_source / ".gemini" / "config" / "skills" / "workflow-template"

    for target in (claude_target, codex_target, gemini_target):
        assert target.is_dir()
        assert not target.is_symlink()
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "template contents"


def test_setup_skills_with_workflow_skill_only_targets_detected_assistants(tmp_path):
    """#394: ホームディレクトリが存在しないアシスタントへは
    プロジェクトローカルコピーも作成しない。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()
    # .codex/.geminiは作成しない

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    (skills_dir / "workflow-template").mkdir()
    (skills_dir / "workflow-template" / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills(with_workflow_skill=True)

    assert (mock_source / ".claude" / "skills" / "workflow-template").is_dir()
    assert not (mock_source / ".codex" / "skills").exists()
    assert not (mock_source / ".gemini").exists()


def test_setup_skills_with_workflow_skill_idempotent_when_already_present(
    tmp_path, capsys
):
    """#394: 既にプロジェクトローカルへ配置済みの場合はスキップとして成功扱い。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    (skills_dir / "workflow-template").mkdir()
    (skills_dir / "workflow-template" / "SKILL.md").touch()

    existing = mock_source / ".claude" / "skills" / "workflow-template"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Skipped" in captured.out


def test_setup_skills_with_workflow_skill_missing_source_fails(tmp_path, capsys):
    """#408: workflow-templateがプロジェクトローカルにも、パッケージ相対の
    フォールバック先にも見つからない場合、クラッシュはしないが、
    --with-workflow-skill単体の失敗として非ゼロ終了コードを返す
    （他のスキルが成功していても、静かにexit 0にはしない）。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    # workflow-templateディレクトリを作成しない

    # パッケージ相対のフォールバック先（#413レビュー対応）にも存在しないことを
    # 保証するため、__file__を無関係な場所へ差し替える。
    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error" in captured.err
    assert "--with-workflow-skill" in captured.err
    assert not (mock_source / ".claude" / "skills" / "workflow-template").exists()


def test_setup_skills_with_workflow_skill_falls_back_to_packaged_source(
    tmp_path, capsys
):
    """PR #413 review: `get_skills_source_dir()` can select a project-local
    `skills/` tree that has `orchestune` but not `workflow-template` (e.g. a
    partial vendor copy) before ever considering the installed package. In
    that case `--with-workflow-skill` must still find the packaged template
    instead of failing outright."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    # cwd's ancestor skills dir: has 'orchestune' but NOT 'workflow-template'.
    mock_cwd = tmp_path / "project"
    mock_cwd.mkdir()
    project_skills_dir = mock_cwd / "skills"
    project_skills_dir.mkdir()
    (project_skills_dir / "orchestune").mkdir()
    (project_skills_dir / "orchestune" / "SKILL.md").touch()

    # Simulated installed package location, with 'workflow-template' present
    # (as a pipx install has after the #408 packaging fix).
    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()
    pkg_skills_dir = fake_pkg_file.parent / "skills"
    pkg_skills_dir.mkdir()
    (pkg_skills_dir / "workflow-template").mkdir()
    (pkg_skills_dir / "workflow-template" / "SKILL.md").write_text(
        "template contents", encoding="utf-8"
    )
    (pkg_skills_dir / "workflow-template" / "references").mkdir()
    (pkg_skills_dir / "workflow-template" / "references" / "tdd.md").write_text(
        "fallback tdd", encoding="utf-8"
    )

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_cwd),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    target = mock_cwd / ".claude" / "skills" / "workflow-template"
    assert target.is_dir()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "template contents"
    assert (target / "references" / "tdd.md").read_text(
        encoding="utf-8"
    ) == "fallback tdd"


def test_setup_skills_with_workflow_skill_missing_source_fails_even_with_no_assistants(
    tmp_path, capsys
):
    """PR #413 review: --with-workflow-skillのテンプレートソースが完全に
    見つからず、かつサポート対象アシスタントのホームディレクトリも一つも
    存在しない場合でも、"No supported AI assistants detected"の早期リターン
    (exit 0)に隠れて、既に出力済みのErrorと矛盾する成功終了コードを
    返してはならない。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    # .claude/.codex/.geminiのいずれも作成しない

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    # workflow-templateディレクトリを作成しない

    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error" in captured.err
    assert "--with-workflow-skill" in captured.err


def test_setup_skills_with_workflow_skill_copies_references_directory(tmp_path):
    """`--with-workflow-skill` 指定時、workflow-template の references/ サブディレクトリ
    およびその中の Markdown ファイル群も含めて完全に実体コピーされることを検証する。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    wf_dir = skills_dir / "workflow-template"
    wf_dir.mkdir()
    (wf_dir / "SKILL.md").write_text("router contents", encoding="utf-8")
    ref_dir = wf_dir / "references"
    ref_dir.mkdir()
    (ref_dir / "tdd.md").write_text("tdd contents", encoding="utf-8")
    (ref_dir / "pr.md").write_text("pr contents", encoding="utf-8")
    (ref_dir / "review-loop.md").write_text("review contents", encoding="utf-8")

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    assert exit_code == 0
    target_skill = mock_source / ".claude" / "skills" / "workflow-template"
    assert target_skill.is_dir()
    assert not target_skill.is_symlink()
    assert (target_skill / "SKILL.md").read_text(encoding="utf-8") == "router contents"

    target_refs = target_skill / "references"
    assert target_refs.is_dir()
    assert (target_refs / "tdd.md").read_text(encoding="utf-8") == "tdd contents"
    assert (target_refs / "pr.md").read_text(encoding="utf-8") == "pr contents"
    assert (target_refs / "review-loop.md").read_text(
        encoding="utf-8"
    ) == "review contents"


def test_setup_skills_with_workflow_skill_distributes_modern_portability_procedures(
    tmp_path,
):
    """`--with-workflow-skill` で配布される workflow-template が、
    preflight、selected backend固定、GitHub MCP post-write検証、bloat baselineの手順を含み、
    旧来のghコマンド固定や旧bloatルールが配布されないことを検証する。"""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    # Orchestuneリポジトリ実体の skills/ ディレクトリを使用
    real_skills_dir = Path(__file__).parents[1] / "skills"
    mock_project = tmp_path / "target_project"
    mock_project.mkdir()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_project),
        patch(
            "orchestune.setup_skills.get_skills_source_dir",
            return_value=real_skills_dir,
        ),
    ):
        exit_code = setup_skills(with_workflow_skill=True)

    assert exit_code == 0
    target_skill = mock_project / ".claude" / "skills" / "workflow-template"
    assert target_skill.is_dir()

    skill_md = (target_skill / "SKILL.md").read_text(encoding="utf-8")
    tdd_md = (target_skill / "references" / "tdd.md").read_text(encoding="utf-8")
    pr_md = (target_skill / "references" / "pr.md").read_text(encoding="utf-8")

    # SKILL.md preflight & backend selection
    assert "Preflight" in skill_md or "preflight" in skill_md
    assert "selected backend" in skill_md.lower()
    # gh固定ではなくselected backend経由になっていること
    assert "selected backend" in skill_md
    assert (
        "<PREFLIGHT_CHECK_COMMAND>" in skill_md
        or "<preflight_check_command>" in skill_md.lower()
    )

    # tdd.md bloat baseline
    assert "baseline" in tdd_md.lower()
    assert "bloat" in tdd_md.lower()
    assert "pre-existing" in tdd_md.lower() or "newly introduced" in tdd_md.lower()

    # pr.md MCP post-write verification & selected backend
    assert "blob" in pr_md.lower()
    assert "sha" in pr_md.lower()
    assert "backend" in pr_md.lower() and "selected" in pr_md.lower()
    assert "cumulative diff" in pr_md.lower()
    assert "head diff" in pr_md.lower()

    # references completeness
    assert (target_skill / "references" / "worktree.md").is_file()
    assert (target_skill / "references" / "review-loop.md").is_file()
