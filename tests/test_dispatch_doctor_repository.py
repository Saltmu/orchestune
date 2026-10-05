from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.dispatch.doctor import DoctorRequest, load_workflow_yaml, run_doctor
from orchestune.dispatch.doctor_models import Diagnostic, DoctorContext, WorkflowFile
from orchestune.dispatch.doctor_repository import run_repository_checks

GROUP = "orchestune-control-${{ github.repository }}"
LEGACY_GROUP = (
    "orchestune-integrate-${{ github.repository }}-${{ inputs.parent_issue }}"
)
CTL = ".github/workflows/ctl.yml"
OTHER = ".github/workflows/other.yml"


def workflow(command: str, group: str | None = GROUP) -> str:
    text = "name: w\non: workflow_dispatch\n"
    if group is not None:
        text += f"concurrency:\n  group: {group}\n  cancel-in-progress: false\n"
    return (
        text
        + f"jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: {command}\n"
    )


def wf(text: str, path: str = OTHER) -> WorkflowFile:
    return WorkflowFile(path, load_workflow_yaml(text), None)


def run(
    mode: str, specified: tuple[WorkflowFile, ...], *others: WorkflowFile
) -> dict[str, Diagnostic]:
    discovered = specified + others
    context = DoctorContext(mode, specified, discovered, {}, True)  # type: ignore[arg-type]
    return {d.code.rsplit(".", 1)[1]: d for d in run_repository_checks(context)}


CTL_OK = wf(workflow("orchestune dispatch -p 1"), CTL)


def test_returns_both_codes_in_order() -> None:
    context = DoctorContext("local", (), (), {}, True)
    codes = [d.code for d in run_repository_checks(context)]
    assert codes == [
        "dispatch.repository.other_entrypoints",
        "dispatch.repository.other_control_entrypoints",
    ]


@pytest.mark.parametrize(
    ("group", "expected"),
    [(GROUP, "warning"), (None, "error"), (LEGACY_GROUP, "error")],
)
def test_actions_other_dispatch(group: str | None, expected: str) -> None:
    other = wf(workflow("orchestune dispatch -p 2", group))
    diag = run("actions", (CTL_OK,), other)["other_entrypoints"]
    assert diag.status == expected
    assert any(OTHER in line for line in diag.evidence)


def test_actions_no_apply_other_is_ok() -> None:
    other = wf(workflow("orchestune dispatch --no-apply", None))
    assert run("actions", (CTL_OK,), other)["other_entrypoints"].status == "ok"


def test_actions_specified_workflow_is_not_counted() -> None:
    assert run("actions", (CTL_OK,))["other_entrypoints"].status == "ok"
    both = run("actions", (CTL_OK,), CTL_OK)["other_entrypoints"]
    assert both.status == "ok"


def test_local_any_dispatch_warns() -> None:
    diag = run("local", (), wf(workflow("orchestune dispatch", None)))
    assert diag["other_entrypoints"].status == "warning"
    assert run("local", (), wf(workflow("echo hi")))["other_entrypoints"].status == "ok"


def test_unreadable_workflow_is_evidence_only() -> None:
    broken = WorkflowFile(OTHER, None, "ScannerError: bad")
    diags = run("actions", (CTL_OK,), broken)
    assert diags["other_entrypoints"].status == "ok"
    assert any("unreadable" in line for line in diags["other_entrypoints"].evidence)


@pytest.mark.parametrize(
    ("command", "group", "expected"),
    [
        ("orchestune gc", GROUP, "ok"),
        ("orchestune gc", LEGACY_GROUP, "warning"),
        ("orchestune recover 12 --apply", None, "warning"),
        ("orchestune gc --no-apply", None, "ok"),
        ("orchestune recover 12", None, "ok"),
    ],
)
def test_control_entrypoints(command: str, group: str | None, expected: str) -> None:
    diag = run("actions", (CTL_OK,), wf(workflow(command, group)))
    assert diag["other_control_entrypoints"].status == expected


def test_control_also_checks_specified_workflow() -> None:
    ctl = wf(workflow("orchestune gc", None), CTL)
    assert run("actions", (ctl,))["other_control_entrypoints"].status == "warning"


def test_control_undetectable_is_not_checked() -> None:
    diag = run("actions", (CTL_OK,), wf(workflow("./scripts/x.sh")))
    assert diag["other_control_entrypoints"].status == "not_checked"


def test_control_warning_outranks_undetectable() -> None:
    other = wf(workflow("orchestune gc", None))
    script = wf(workflow("./scripts/x.sh"), ".github/workflows/s.yml")
    diag = run("actions", (CTL_OK,), other, script)["other_control_entrypoints"]
    assert diag.status == "warning"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_doctor_fails_on_unsafe_other_workflow(tmp_path: Path) -> None:
    _write(tmp_path, CTL, workflow("orchestune dispatch -p 1"))
    _write(tmp_path, OTHER, workflow("orchestune dispatch -p 2", LEGACY_GROUP))
    report = run_doctor(DoctorRequest("actions", tmp_path, (CTL,)))
    by_code = {d.code: d for d in report.diagnostics}
    assert by_code["dispatch.repository.other_entrypoints"].status == "error"
    assert report.has_error
    codes = [d.code for d in report.diagnostics]
    assert codes.index("dispatch.repository.other_control_entrypoints") < codes.index(
        "dispatch.external_ownership"
    )


def test_run_doctor_local_mode_warns(tmp_path: Path) -> None:
    _write(tmp_path, OTHER, workflow("orchestune dispatch", None))
    report = run_doctor(DoctorRequest("local", tmp_path))
    by_code = {d.code: d for d in report.diagnostics}
    assert by_code["dispatch.repository.other_entrypoints"].status == "warning"
    assert not report.has_error
