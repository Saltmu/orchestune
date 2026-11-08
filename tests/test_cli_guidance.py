"""Human rendering of operator guidance, including shell quoting."""

from orchestune.infra.cli_guidance import quote_command, render_guidance
from orchestune.operator_guidance import (
    EvidenceState,
    GuidanceAction,
    NextOwner,
    OperatorGuidance,
    ProgressOutlook,
)


def _guidance(**overrides: object) -> OperatorGuidance:
    values: dict[str, object] = {
        "target": "trial abc",
        "stage": "planned",
        "reason_codes": ("approval_required",),
        "explanation": "計画の承認が必要です",
        "next_owner": NextOwner.USER,
        "progress": ProgressOutlook.MANUAL,
        "progress_condition": None,
        "actions": (
            GuidanceAction(
                description="承認する",
                argv=("orchestune", "trial", "resume", "abc", "--repo", "my dir"),
            ),
        ),
        "references": ("docs/ja/usage.md",),
        "evidence_state": EvidenceState.CURRENT,
        "evidence_note": "この表示回です",
    }
    values.update(overrides)
    return OperatorGuidance(**values)  # type: ignore[arg-type]


def test_render_keeps_the_common_fields() -> None:
    text = render_guidance(_guidance())
    assert "対象: trial abc" in text
    assert "段階: planned" in text
    assert "理由: approval_required（計画の承認が必要です）" in text
    assert "次の担当: user" in text
    assert "自動進行: manual" in text
    assert "根拠: current（この表示回です）" in text
    assert "参照: docs/ja/usage.md" in text


def test_unknown_fields_stay_visible() -> None:
    text = render_guidance(
        _guidance(
            reason_codes=(),
            explanation="",
            next_owner=NextOwner.UNKNOWN,
            progress=ProgressOutlook.UNKNOWN,
            actions=(),
            references=(),
        )
    )
    assert "理由: 不明" in text
    assert "次の担当: unknown" in text
    assert "自動進行: unknown" in text
    assert "次の操作: なし" in text


def test_conditional_progress_includes_its_condition() -> None:
    text = render_guidance(
        _guidance(
            progress=ProgressOutlook.CONDITIONAL,
            progress_condition="dispatcher の稼働は未観測です",
            actions=(),
        )
    )
    assert "自動進行: conditional（dispatcher の稼働は未観測です）" in text


def test_placeholder_is_not_an_executable_command() -> None:
    text = render_guidance(
        _guidance(
            actions=(
                GuidanceAction(
                    description="claim する",
                    argv=("orchestune", "claim", "<issue>"),
                ),
            )
        )
    )
    assert "コマンド:" not in text
    assert "未解決の値があるためコマンドとしては表示しません" in text


def test_posix_and_windows_quote_paths_with_spaces() -> None:
    argv = ("orchestune", "trial", "resume", "abc", "--repo", r"C:\work\my dir")
    posix = quote_command(argv, posix=True)
    windows = quote_command(argv, posix=False)
    assert "C:/work/my dir" in posix
    assert posix.startswith("orchestune trial resume abc --repo ")
    assert "'" in posix
    assert "C:/work/my dir" in windows
    assert '"' in windows
