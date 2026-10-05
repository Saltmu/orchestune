from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestune.dispatch.doctor import DoctorRequest, load_workflow_yaml, run_doctor
from orchestune.dispatch.doctor_actions import (
    STANDARD_GROUP,
    is_standard_group,
    normalize_group_expression,
    run_actions_checks,
)
from orchestune.dispatch.doctor_models import DoctorContext, WorkflowFile

GROUP = "orchestune-control-${{ github.repository }}"


def make_workflow(
    *,
    concurrency: str | None = f"  group: {GROUP}\n  cancel-in-progress: false",
    command: str = "orchestune dispatch -p 1 --dispatch-target cloud-routine",
    top_env: str = "",
    job_env: str = "",
    step_env: str = (
        "          ORCHESTUNE_ROUTINE_ID: ${{ vars.ID }}\n"
        "          ORCHESTUNE_ROUTINE_TOKEN: ${{ secrets.TOKEN }}"
    ),
    extra_job: str = "",
    extra_jobs: str = "",
) -> str:
    text = "name: ctl\non: workflow_dispatch\n"
    if concurrency is not None:
        sep = "\n" if concurrency.startswith("  ") else ""
        text += f"concurrency:{sep}{concurrency}\n"
    text += top_env
    text += "jobs:\n  ctl:\n    runs-on: ubuntu-latest\n" + job_env + extra_job
    text += "    steps:\n      - run: " + command + "\n"
    if step_env:
        text += "        env:\n" + step_env + "\n"
    return text + extra_jobs


def wf(text: str, path: str = ".github/workflows/ctl.yml") -> WorkflowFile:
    return WorkflowFile(path, load_workflow_yaml(text), None)


def checks(
    *files: str | WorkflowFile,
    config: dict[str, Any] | None = None,
    config_valid: bool = True,
) -> dict[str, Any]:
    specified = tuple(f if isinstance(f, WorkflowFile) else wf(f) for f in files)
    context = DoctorContext("actions", specified, (), config or {}, config_valid)
    return {d.code.rsplit(".", 1)[1]: d for d in run_actions_checks(context)}


def status(text: str, name: str, **kwargs: Any) -> str:
    return str(checks(text, **kwargs)[name].status)


def test_passing_workflow_is_all_ok() -> None:
    result = checks(make_workflow())
    assert list(result) == [
        "group",
        "cancel",
        "parallelism",
        "entrypoint",
        "target",
        "credentials",
    ]
    assert {d.status for d in result.values()} == {"ok"}


def test_no_readable_workflow_is_not_checked() -> None:
    context = DoctorContext(
        "actions", (WorkflowFile("x.yml", None, "bad"),), (), {}, True
    )
    diags = run_actions_checks(context)
    assert len(diags) == 6
    assert {d.status for d in diags} == {"not_checked"}
    assert diags[0].evidence == ("no readable workflow",)


@pytest.mark.parametrize(
    "concurrency",
    [
        None,
        " orchestune-control-${{ github.repository }}",
        "  group: ''\n  cancel-in-progress: false",
        f"  group: {GROUP}-${{{{ inputs.parent_issue }}}}\n  cancel-in-progress: false",
        f"  group: {GROUP}-${{{{ github.workflow }}}}\n  cancel-in-progress: false",
        f"  group: {GROUP}-${{{{ github.ref }}}}\n  cancel-in-progress: false",
        f"  group: {GROUP}-${{{{ github.run_id }}}}\n  cancel-in-progress: false",
        "  group: orchestune-integrate-${{ github.repository }}-${{ inputs.parent_issue }}\n"
        "  cancel-in-progress: false",
        "  cancel-in-progress: false",
    ],
)
def test_group_errors(concurrency: str | None) -> None:
    assert status(make_workflow(concurrency=concurrency), "group") == "error"


def test_group_ignores_inner_expression_whitespace() -> None:
    text = make_workflow(
        concurrency="  group: orchestune-control-${{github.repository}}\n"
        "  cancel-in-progress: false"
    )
    assert status(text, "group") == "ok"


def test_group_mismatch_between_workflows_is_error() -> None:
    other = make_workflow(concurrency="  group: other\n  cancel-in-progress: false")
    result = checks(make_workflow(), wf(other, ".github/workflows/b.yml"))
    assert result["group"].status == "error"
    assert any("b.yml" in line and "other" in line for line in result["group"].evidence)


@pytest.mark.parametrize("cancel", [None, "true", '"false"', "${{ false }}", "0"])
def test_cancel_errors(cancel: str | None) -> None:
    body = f"  group: {GROUP}"
    if cancel is not None:
        body += f"\n  cancel-in-progress: {cancel}"
    assert status(make_workflow(concurrency=body), "cancel") == "error"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"extra_job": "    strategy:\n      matrix:\n        n: [1, 2]\n"},
        {"extra_job": "    concurrency: other\n"},
        {
            "extra_jobs": "  second:\n    runs-on: x\n    steps:\n"
            "      - run: orchestune dispatch --dispatch-target cloud-routine\n"
        },
        {"command": "orchestune dispatch --dispatch-target cloud-routine &"},
        {"command": "nohup orchestune dispatch --dispatch-target cloud-routine"},
    ],
)
def test_parallelism_errors(kwargs: dict[str, str]) -> None:
    assert status(make_workflow(**kwargs), "parallelism") == "error"


def test_parallelism_without_entrypoint_is_not_checked() -> None:
    assert status(make_workflow(command="echo hi"), "parallelism") == "not_checked"


def test_entrypoint_wrapper_script_is_not_checked() -> None:
    result = checks(make_workflow(command="./scripts/dispatch.sh"))
    assert result["entrypoint"].status == "not_checked"
    assert result["parallelism"].status == "not_checked"


def test_entrypoint_with_undetectable_other_job_is_not_checked() -> None:
    other = "  other:\n    runs-on: x\n    steps:\n      - uses: ./.github/actions/x\n"
    assert status(make_workflow(extra_jobs=other), "entrypoint") == "not_checked"


def test_no_apply_is_not_counted() -> None:
    result = checks(make_workflow(command="orchestune dispatch --no-apply"))
    assert result["entrypoint"].status == "not_checked"
    assert any("not counted" in line for line in result["entrypoint"].evidence)


@pytest.mark.parametrize(
    "target", ["local", "claude-cli", "agy-cli", "codex-cli", "auto"]
)
def test_local_process_target_argument_is_error(target: str) -> None:
    text = make_workflow(command=f"orchestune dispatch --dispatch-target {target}")
    assert status(text, "target") == "error"


def test_target_from_config_then_actions_default() -> None:
    text = make_workflow(command="orchestune dispatch -p 1")
    assert status(text, "target", config={"dispatch_target": "local"}) == "error"
    assert status(text, "target", config={"dispatch_target": "codex-cloud"}) == "ok"
    assert status(text, "target") == "ok"


def test_argument_wins_over_config() -> None:
    text = make_workflow(command="orchestune dispatch --dispatch-target cloud-routine")
    assert status(text, "target", config={"dispatch_target": "local"}) == "ok"


def test_dynamic_target_and_invalid_config_are_not_checked() -> None:
    dynamic = make_workflow(command='orchestune dispatch --dispatch-target "$T"')
    assert status(dynamic, "target") == "not_checked"
    plain = make_workflow(command="orchestune dispatch -p 1")
    assert status(plain, "target", config_valid=False) == "not_checked"


def test_credentials_missing_token_is_error() -> None:
    text = make_workflow(step_env="          ORCHESTUNE_ROUTINE_ID: x")
    assert status(text, "credentials") == "error"


def test_credentials_routine_id_from_config() -> None:
    text = make_workflow(
        step_env="          ORCHESTUNE_ROUTINE_TOKEN: ${{ secrets.T }}"
    )
    assert status(text, "credentials") == "error"
    assert status(text, "credentials", config={"routine_id": "r1"}) == "ok"


def test_credentials_empty_or_null_value_is_error() -> None:
    text = make_workflow(
        step_env="          ORCHESTUNE_ROUTINE_ID: ''\n          ORCHESTUNE_ROUTINE_TOKEN:"
    )
    assert status(text, "credentials") == "error"


def test_credentials_at_workflow_and_job_level() -> None:
    env = "ORCHESTUNE_ROUTINE_ID: a\n{i}ORCHESTUNE_ROUTINE_TOKEN: b\n"
    top = make_workflow(step_env="", top_env="env:\n  " + env.format(i="  "))
    assert status(top, "credentials") == "ok"
    job = make_workflow(
        step_env="", job_env="    env:\n      " + env.format(i="      ")
    )
    assert status(job, "credentials") == "ok"


def test_credentials_env_expression_is_not_checked() -> None:
    text = make_workflow(step_env="", job_env="    env: ${{ fromJSON(vars.X) }}\n")
    assert status(text, "credentials") == "not_checked"


def test_credentials_codex_cloud() -> None:
    cmd = "orchestune dispatch --dispatch-target codex-cloud"
    text = make_workflow(command=cmd, step_env="          OTHER: x")
    assert status(text, "credentials") == "error"
    assert status(text, "credentials", config={"codex_cloud_env": "e"}) == "ok"
    with_env = make_workflow(
        command=cmd, step_env="          ORCHESTUNE_CODEX_CLOUD_ENV: e"
    )
    assert status(with_env, "credentials") == "ok"


def test_credentials_with_error_target_is_not_checked_and_hides_values() -> None:
    local = make_workflow(command="orchestune dispatch --dispatch-target local")
    assert status(local, "credentials") == "not_checked"
    result = checks(
        make_workflow(
            step_env="          ORCHESTUNE_ROUTINE_ID: ${{ secrets.SECRET_X }}"
        )
    )
    assert "SECRET_X" not in " ".join(result["credentials"].evidence)


def test_group_helpers() -> None:
    assert (
        normalize_group_expression("a-${{github.x}}-${{  b }}")
        == "a-${{ github.x }}-${{ b }}"
    )
    assert is_standard_group(load_workflow_yaml(make_workflow()))
    assert not is_standard_group(load_workflow_yaml(make_workflow(concurrency=" x")))
    assert STANDARD_GROUP == GROUP


def test_run_doctor_includes_actions_checks_only_in_actions_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".github" / "workflows" / "ctl.yml"
    path.parent.mkdir(parents=True)
    path.write_text(make_workflow(), encoding="utf-8")
    actions = run_doctor(
        DoctorRequest("actions", tmp_path, (".github/workflows/ctl.yml",))
    )
    codes = [d.code for d in actions.diagnostics]
    assert all(
        f"dispatch.actions.{n}" in codes
        for n in (
            "group",
            "cancel",
            "parallelism",
            "entrypoint",
            "target",
            "credentials",
        )
    )
    local = run_doctor(DoctorRequest("local", tmp_path))
    assert not any(d.code.startswith("dispatch.actions.") for d in local.diagnostics)


def test_credentials_empty_lower_scope_overrides_higher_scope() -> None:
    top = "env:\n  ORCHESTUNE_ROUTINE_ID: a\n  ORCHESTUNE_ROUTINE_TOKEN: b\n"
    text = make_workflow(top_env=top, step_env="          ORCHESTUNE_ROUTINE_TOKEN: ''")
    assert status(text, "credentials") == "error"
    assert status(make_workflow(top_env=top, step_env=""), "credentials") == "ok"
