"""Tests for interactive configuration wizard flow and PromptPort interaction."""

from pathlib import Path

from orchestune.config_wizard.wizard import (
    PromptPort,
    run_config_wizard,
)


class MockPromptPort(PromptPort):
    """Test helper that returns scripted responses."""

    def __init__(
        self,
        text_responses: list[str | None] | None = None,
        choice_responses: list[str | None] | None = None,
        integer_responses: list[int | None] | None = None,
        confirm_responses: list[bool] | None = None,
    ):
        self.text_responses = list(text_responses or [])
        self.choice_responses = list(choice_responses or [])
        self.integer_responses = list(integer_responses or [])
        self.confirm_responses = list(confirm_responses or [])
        self.messages: list[str] = []
        self.diffs: list[str] = []

    def prompt_text(
        self, message: str, default: str | None = None, allow_empty: bool = True
    ) -> str | None:
        if self.text_responses:
            return self.text_responses.pop(0)
        return default

    def prompt_choice(
        self,
        message: str,
        choices: list[str],
        default: str | None = None,
        allow_empty: bool = True,
    ) -> str | None:
        if self.choice_responses:
            return self.choice_responses.pop(0)
        return default

    def prompt_integer(
        self,
        message: str,
        default: int | None = None,
        min_val: int | None = None,
        max_val: int | None = None,
        allow_empty: bool = True,
    ) -> int | None:
        if self.integer_responses:
            return self.integer_responses.pop(0)
        return default

    def prompt_array(
        self, message: str, current: list[str] | None = None
    ) -> list[str] | None:
        return current

    def prompt_confirm(self, message: str, default: bool = False) -> bool:
        if self.confirm_responses:
            return self.confirm_responses.pop(0)
        return default

    def display_message(self, msg: str) -> None:
        self.messages.append(msg)

    def display_diff(self, diff_str: str) -> None:
        self.diffs.append(diff_str)


class TestConfigWizardFlow:
    def test_init_basic_answers_and_save(self, tmp_path: Path):
        prompt = MockPromptPort(
            # Basic questions:
            # dispatch-target
            choice_responses=["auto", "preview", "save"],
            # ci-command
            text_responses=["pytest -v"],
            # max-concurrent, max-launches, window-seconds
            integer_responses=[2, None, 3600],
            # reviewer-bot choice (handled by choice_responses or separate)
        )
        # We need to script responses matching wizard's flow:
        # 1. dispatch-target -> 'auto'
        # 2. ci-command -> 'pytest -v'
        # 3. max-concurrent -> 2
        # 4. max-launches-per-window -> None
        # 5. window-seconds -> 3600
        # 6. reviewer-bot -> 'claude'
        # 7. main menu -> 'preview'
        # 8. confirm action -> 'save'
        prompt.choice_responses = ["auto", "claude", "preview", "save"]
        prompt.text_responses = ["pytest -v"]
        prompt.integer_responses = [2, None, 3600]

        result = run_config_wizard(mode="init", project_dir=tmp_path, prompt=prompt)
        assert result.status == "saved"
        assert (tmp_path / "orchestune.toml").exists()
        saved_text = (tmp_path / "orchestune.toml").read_text(encoding="utf-8")
        assert 'dispatch-target = "auto"' in saved_text
        assert 'ci-command = "pytest -v"' in saved_text
        assert "max-concurrent = 2" in saved_text

    def test_init_cancel_at_confirmation(self, tmp_path: Path):
        prompt = MockPromptPort(
            choice_responses=["auto", "claude", "preview", "cancel"],
            text_responses=["pytest"],
            integer_responses=[2, None, 3600],
        )
        result = run_config_wizard(mode="init", project_dir=tmp_path, prompt=prompt)
        assert result.status == "cancelled"
        assert not (tmp_path / "orchestune.toml").exists()

    def test_edit_unchanged_detected(self, tmp_path: Path):
        target = tmp_path / "orchestune.toml"
        content = 'dispatch-target = "auto"\nci-command = "pytest"\nmax-concurrent = 2\nwindow-seconds = 3600\n'
        target.write_text(content, encoding="utf-8")

        prompt = MockPromptPort(
            # Press enter / keep existing for all
            choice_responses=["auto", None, "preview"],
            text_responses=["pytest"],
            integer_responses=[2, None, 3600],
        )
        result = run_config_wizard(mode="edit", project_dir=tmp_path, prompt=prompt)
        assert result.status == "unchanged"

    def test_ctrl_c_handled_as_cancelled(self, tmp_path: Path):
        class InterruptPrompt(MockPromptPort):
            def prompt_choice(self, *args, **kwargs):
                raise KeyboardInterrupt()

        prompt = InterruptPrompt()
        result = run_config_wizard(mode="init", project_dir=tmp_path, prompt=prompt)
        assert result.status == "cancelled"
        assert not (tmp_path / "orchestune.toml").exists()

    def test_eof_handled_as_cancelled(self, tmp_path: Path):
        class EofPrompt(MockPromptPort):
            def prompt_choice(self, *args, **kwargs):
                raise EOFError()

        prompt = EofPrompt()
        result = run_config_wizard(mode="init", project_dir=tmp_path, prompt=prompt)
        assert result.status == "cancelled"
        assert not (tmp_path / "orchestune.toml").exists()
