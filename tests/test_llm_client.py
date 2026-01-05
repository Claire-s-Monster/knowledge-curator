"""Tests for LLM client API key validation."""

from pathlib import Path

import pytest

from knowledge_curator.config import Settings, reset_settings
from knowledge_curator.llm.client import CuratorLLMClient


class TestLLMClientValidation:
    """Tests for LLM client initialization and validation."""

    def test_init_with_valid_api_key(self) -> None:
        """Should initialize successfully with valid API key."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )

        # Should not raise
        client = CuratorLLMClient(settings)
        assert client is not None

    def test_init_without_api_key_raises(self) -> None:
        """Should raise ValueError when API key is not configured."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="",
            db_path=Path(":memory:"),
        )

        with pytest.raises(ValueError, match="Anthropic API key not configured"):
            CuratorLLMClient(settings)

    def test_init_with_whitespace_api_key_raises(self) -> None:
        """Should raise ValueError when API key is only whitespace."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="   ",
            db_path=Path(":memory:"),
        )

        with pytest.raises(ValueError, match="Anthropic API key not configured"):
            CuratorLLMClient(settings)

    def test_has_api_key_property(self) -> None:
        """Should correctly report API key status."""
        reset_settings()

        # With key
        settings_with_key = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        assert settings_with_key.has_api_key is True

        # Without key
        settings_no_key = Settings(
            anthropic_api_key="",
            db_path=Path(":memory:"),
        )
        assert settings_no_key.has_api_key is False

        # Whitespace only
        settings_whitespace = Settings(
            anthropic_api_key="   ",
            db_path=Path(":memory:"),
        )
        assert settings_whitespace.has_api_key is False
