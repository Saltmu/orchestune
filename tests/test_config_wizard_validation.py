"""Tests for configuration validation adapters and shared CI command parsing."""

import pytest

from orchestune.dag.models import ConfigError
from orchestune.dispatch.config_loader import (
    parse_ci_command,
    validate_toml_config,
)


class TestParseCiCommand:
    def test_parse_valid_string(self):
        assert parse_ci_command("pytest -v --cov") == ["pytest", "-v", "--cov"]

    def test_parse_valid_list(self):
        assert parse_ci_command(["pytest", "-v", "--cov"]) == [
            "pytest",
            "-v",
            "--cov",
        ]

    def test_parse_empty_string_raises(self):
        with pytest.raises(ConfigError, match="must not be empty"):
            parse_ci_command("")
        with pytest.raises(ConfigError, match="must not be empty"):
            parse_ci_command("   ")

    def test_parse_unclosed_quote_raises(self):
        with pytest.raises(ConfigError, match="invalid ci-command"):
            parse_ci_command("pytest 'unclosed quote")

    def test_parse_empty_list_raises(self):
        with pytest.raises(ConfigError, match="must not be empty"):
            parse_ci_command([])

    def test_parse_list_with_empty_or_non_string_element_raises(self):
        with pytest.raises(ConfigError, match="non-empty strings"):
            parse_ci_command(["pytest", ""])
        with pytest.raises(ConfigError, match="non-empty strings"):
            parse_ci_command(["pytest", 123])  # type: ignore

    def test_parse_invalid_type_raises(self):
        with pytest.raises(ConfigError, match="string or list of strings"):
            parse_ci_command(123)  # type: ignore


class TestValidateTomlConfigSharedEnhancements:
    def test_validate_toml_ci_command_string_valid(self):
        data = {"ci-command": "pytest -v"}
        validated = validate_toml_config(data)
        assert validated["ci_command"] == "pytest -v"
        assert validated["ci-command"] == "pytest -v"

    def test_validate_toml_ci_command_list_valid(self):
        data = {"ci_command": ["pytest", "-v"]}
        validated = validate_toml_config(data)
        assert validated["ci_command"] == ["pytest", "-v"]

    def test_validate_toml_ci_command_empty_string(self):
        with pytest.raises(ConfigError, match="must not be empty"):
            validate_toml_config({"ci-command": ""})

    def test_validate_toml_ci_command_unclosed_quote(self):
        with pytest.raises(ConfigError, match="invalid ci-command"):
            validate_toml_config({"ci-command": "pytest 'foo"})

    def test_validate_toml_ci_command_empty_list(self):
        with pytest.raises(ConfigError, match="must not be empty"):
            validate_toml_config({"ci-command": []})

    def test_validate_toml_ci_command_empty_element(self):
        with pytest.raises(ConfigError, match="non-empty strings"):
            validate_toml_config({"ci-command": ["pytest", ""]})

    def test_validate_toml_default_execution_profile_alone_valid(self):
        data = {"default_execution_profile": "custom_profile"}
        validated = validate_toml_config(data)
        assert validated["default_execution_profile"] == "custom_profile"

    def test_validate_toml_default_execution_profile_alone_invalid_name(self):
        with pytest.raises(ConfigError, match="invalid execution profile name"):
            validate_toml_config({"default_execution_profile": "-invalid-name"})

    def test_validate_toml_default_execution_profile_hyphen_key_invalid(self):
        with pytest.raises(ConfigError, match="invalid execution profile name"):
            validate_toml_config(
                {"default-execution-profile": "invalid name with space"}
            )


class TestConfigWizardValidationAdapters:
    def test_diagnose_prohibited_key_redacts_value(self):
        from orchestune.config_wizard.validation import diagnose_existing_document

        data = {"routine_token": "super-secret-token", "ci-command": "pytest"}
        errors = diagnose_existing_document(data)
        assert len(errors) == 1
        assert "routine_token" in errors[0]
        assert "super-secret-token" not in errors[0]

    def test_diagnose_unknown_key_redacts_value(self):
        from orchestune.config_wizard.validation import diagnose_existing_document

        data = {"unknown_future_key": "secret-value"}
        errors = diagnose_existing_document(data)
        assert len(errors) == 1
        assert "unknown_future_key" in errors[0]
        assert "secret-value" not in errors[0]

    def test_validate_candidate_document_valid(self):
        from orchestune.config_wizard.validation import validate_candidate_document

        valid_toml = (
            "dispatch-target = 'local'\n"
            "ci-command = 'pytest'\n"
            "max-concurrent = 2\n"
        )
        ok, errors = validate_candidate_document(valid_toml)
        assert ok is True
        assert errors == []

    def test_validate_candidate_document_invalid(self):
        from orchestune.config_wizard.validation import validate_candidate_document

        invalid_toml = "dispatch-target = 'invalid-target'\n" "window-seconds = -10\n"
        ok, errors = validate_candidate_document(invalid_toml)
        assert ok is False
        assert len(errors) >= 1
