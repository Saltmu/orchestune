from __future__ import annotations

import builtins
import subprocess

import pytest

from orchestune.dispatch.doctor_entrypoints import scan_workflow


def _scan(run: str):
    doc = {"jobs": {"j": {"steps": [{"run": run}]}}}
    return scan_workflow("wf.yml", doc)


@pytest.mark.parametrize(
    "run",
    [
        "orchestune dispatch -p 1",
        "uv run orchestune dispatch -p 1",
        "uvx orchestune dispatch",
        "poetry run orchestune dispatch",
        "pipx run orchestune dispatch",
        "cd repo && orchestune dispatch -p 1",
        "FOO=1 orchestune dispatch",
    ],
)
def test_detects_orchestune_dispatch(run):
    scan = _scan(run)
    assert len(scan.dispatch) == 1
    entry = scan.dispatch[0]
    assert entry.via == "orchestune"
    assert entry.apply is True
    assert entry.dispatch_target is None
    assert entry.args_error is None


def test_orchestune_dispatch_binary_and_no_apply():
    entry = _scan("orchestune-dispatch --no-apply").dispatch[0]
    assert entry.via == "orchestune-dispatch"
    assert entry.apply is False


@pytest.mark.parametrize(
    "run",
    [
        "python -m orchestune.dispatch.dispatcher",
        "python3 -m orchestune.dispatch.dispatcher",
        "uv run python -morchestune.dispatch.dispatcher",
    ],
)
def test_detects_python_module(run):
    assert [e.via for e in _scan(run).dispatch] == ["python-m"]


def test_line_continuation_is_joined():
    scan = _scan("echo hi\norchestune dispatch \\\n  --dispatch-target=local")
    (entry,) = scan.dispatch
    assert entry.dispatch_target == "local"
    assert entry.location.line_no == 2
    assert entry.location.step_index == 0


def test_abbreviations_and_last_wins():
    assert _scan("orchestune dispatch --no-ap").dispatch[0].apply is False
    assert _scan("orchestune dispatch --no-apply --apply").dispatch[0].apply is True
    entry = _scan("orchestune dispatch --dispatch-t codex-cloud").dispatch[0]
    assert entry.dispatch_target == "codex-cloud"
    assert entry.args_error is None


@pytest.mark.parametrize(
    "run",
    [
        'orchestune dispatch --dispatch-target "$T"',
        "orchestune dispatch --dispatch-target ${{ inputs.t }}",
    ],
)
def test_dynamic_target(run):
    entry = _scan(run).dispatch[0]
    assert entry.target_is_dynamic is True
    assert entry.args_error is None


def test_dynamic_parent_value_is_not_an_error():
    assert _scan('orchestune dispatch -p "$PARENT"').dispatch[0].args_error is None


@pytest.mark.parametrize(
    "args",
    ["--dispatch-target bogus", "--max-concurrent", "--unknown"],
)
def test_args_errors_keep_apply_true(args):
    entry = _scan(f"orchestune dispatch {args}").dispatch[0]
    assert entry.args_error
    assert entry.apply is True


@pytest.mark.parametrize(
    "run",
    [
        "orchestune dispatch -p 1 &",
        "nohup orchestune dispatch",
        "(orchestune dispatch -p 1) &",
        "setsid orchestune dispatch",
        "orchestune dispatch & disown",
    ],
)
def test_background(run):
    assert _scan(run).dispatch[0].background is True


@pytest.mark.parametrize(
    "run",
    [
        "orchestune dispatch -p 1 >result.log 2>&1",
        "orchestune dispatch -p 1 > out.log 2>&1 | tee x",
        "orchestune dispatch -p 1 &>out.log",
    ],
)
def test_redirections_are_ignored(run):
    entry = _scan(run).dispatch[0]
    assert entry.args_error is None
    assert entry.background is False


def test_redirection_chars_inside_quotes_are_kept():
    assert (
        _scan('orchestune dispatch --dispatch-target "a>b"').dispatch[0].dispatch_target
        == "a>b"
    )


def test_foreground_is_not_background():
    assert _scan("orchestune dispatch -p 1").dispatch[0].background is False


@pytest.mark.parametrize(
    "run",
    ['echo "orchestune dispatch"', "orchestune skills doctor", "orchestune dispatchx"],
)
def test_not_detected(run):
    scan = _scan(run)
    assert not scan.dispatch and not scan.control and not scan.undetectable


@pytest.mark.parametrize(
    "run", ["$CMD dispatch", '"$TOOL" -p 1', "${{ inputs.cmd }} -p 1"]
)
def test_command_expansion(run):
    assert [u.kind for u in _scan(run).undetectable] == ["command_expansion"]


@pytest.mark.parametrize(
    "run", ['case "$x" in *" $y "*) echo hi ;; esac', "index=$((index + 1))"]
)
def test_shell_noise_is_not_undetectable(run):
    scan = _scan(run)
    assert not scan.undetectable and not scan.dispatch


@pytest.mark.parametrize("run", ['bash -c "orchestune dispatch"', 'eval "$X"'])
def test_nested_shell(run):
    assert [u.kind for u in _scan(run).undetectable] == ["nested_shell"]


@pytest.mark.parametrize(
    "run", ["./scripts/run.sh", "bash deploy.sh", "python tools/x.py"]
)
def test_script(run):
    assert [u.kind for u in _scan(run).undetectable] == ["script"]


def test_python_dash_m_other_module_is_not_script():
    assert not _scan("python -m pytest").undetectable


def test_unparsable_line():
    assert [u.kind for u in _scan('echo "unterminated').undetectable] == [
        "unparsable_line"
    ]


def test_uses_classification():
    doc = {
        "jobs": {
            "a": {
                "steps": [
                    {"uses": "./.github/actions/x"},
                    {"uses": "docker://alpine"},
                    {"uses": "actions/checkout@v4"},
                ]
            },
            "b": {"uses": "org/repo/.github/workflows/x.yml@main"},
        }
    }
    scan = scan_workflow("wf.yml", doc)
    assert [(u.kind, u.location.step_index) for u in scan.undetectable] == [
        ("local_action", 0),
        ("docker_action", 1),
        ("reusable_workflow", -1),
    ]


def test_gc_control():
    assert [c.kind for c in _scan("orchestune gc").control] == ["gc"]
    assert not _scan("orchestune gc --no-apply").control


@pytest.mark.parametrize(
    "run",
    [
        "orchestune recover 12 --apply",
        "orchestune recover 12 --ap",
        "orchestune recover --a",
    ],
)
def test_recover_apply(run):
    assert [c.kind for c in _scan(run).control] == ["recover"]


def test_recover_preview_is_not_control():
    assert not _scan("orchestune recover 12").control


def test_malformed_documents_are_tolerated():
    assert scan_workflow("w", {}).jobs == ()
    assert (
        scan_workflow("w", {"jobs": {"a": 1, "b": {"steps": [3, {"run": 1}]}}}).dispatch
        == ()
    )


def test_scan_is_pure(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("impure")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(builtins, "open", boom)
    assert len(_scan("orchestune dispatch").dispatch) == 1
