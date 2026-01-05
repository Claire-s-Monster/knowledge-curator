"""Tests for configuration loading."""

from pathlib import Path

import pytest

from knowledge_curator.config import Settings, get_settings, reset_settings


class TestSettings:
    """Tests for Settings class."""

    def setup_method(self) -> None:
        """Reset settings before each test."""
        reset_settings()

    def test_default_settings(self) -> None:
        """Test default settings values."""
        settings = Settings()

        assert settings.webhook_port == 4005
        assert settings.webhook_host == "127.0.0.1"
        assert settings.log_level == "INFO"
        assert settings.models.default == "claude-sonnet-4-20250514"
        assert settings.thresholds.similarity_duplicate == 0.95
        assert settings.retry.max_attempts == 3

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test environment variable overrides."""
        monkeypatch.setenv("CURATOR_WEBHOOK_PORT", "9999")
        monkeypatch.setenv("CURATOR_LOG_LEVEL", "DEBUG")

        settings = Settings()

        assert settings.webhook_port == 9999
        assert settings.log_level == "DEBUG"

    def test_nested_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test nested configuration via environment."""
        monkeypatch.setenv("CURATOR_MODELS__DEFAULT", "custom-model")

        settings = Settings()

        assert settings.models.default == "custom-model"

    def test_get_settings_singleton(self) -> None:
        """Test get_settings returns same instance."""
        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2

    def test_reset_settings(self) -> None:
        """Test reset_settings clears singleton."""
        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()

        assert settings1 is not settings2
