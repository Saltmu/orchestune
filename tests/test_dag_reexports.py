"""DAG integration coverage for statically declared ImportFrom re-exports."""

from pathlib import Path

from orchestune.dag.graph import build_dag
from orchestune.dag.models import SubTask


def test_build_dag_accepts_public_reexports_and_warns_for_unknown_names(
    tmp_path: Path,
) -> None:
    facade = tmp_path / "pkg" / "facade.py"
    facade.parent.mkdir()
    facade.write_text(
        "from missing_contracts import Foo as PublicFoo\n" '__all__ = ["PublicFoo"]\n',
        encoding="utf-8",
    )
    subtask = SubTask(
        id="task-a",
        description="",
        footprint=("pkg/facade.py",),
        symbols=("PublicFoo", "MissingSymbol"),
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )

    dag = build_dag([subtask], repo_root=tmp_path)

    assert len(dag.warnings) == 1
    assert "MissingSymbol" in dag.warnings[0]
    assert "PublicFoo" not in dag.warnings[0]
