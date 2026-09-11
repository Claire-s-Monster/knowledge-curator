"""Configuration management for Knowledge Curator daemon.

Loads configuration from environment variables and optional YAML config file.
Environment variables take precedence over config file values.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelConfig(BaseSettings):
    """LLM model configuration."""

    default: str = Field(
        default="sonnet",
        description="Default model for curation tasks",
    )
    simple_tasks: str = Field(
        default="haiku",
        description="Model for simple classification tasks",
    )
    complex_tasks: str = Field(
        default="opus",
        description="Model for complex synthesis tasks",
    )


class ThresholdsConfig(BaseSettings):
    """Decision thresholds configuration."""

    similarity_duplicate: float = Field(
        default=0.95,
        description="Cosine similarity threshold for duplicate detection",
    )
    similarity_related: float = Field(
        default=0.85,
        description="Cosine similarity threshold for related entries",
    )
    success_rate_canonical: float = Field(
        default=0.8,
        description="Success rate threshold for canonical status",
    )
    success_rate_archive: float = Field(
        default=0.3,
        description="Success rate threshold for archiving",
    )
    min_applications_canonical: int = Field(
        default=5,
        description="Minimum applications before canonical consideration",
    )
    days_unused_archive: int = Field(
        default=30,
        description="Days unused before archive consideration",
    )


class RateLimitConfig(BaseSettings):
    """Rate limiting configuration."""

    daily_budget_usd: float = Field(
        default=10.0,
        description="Maximum daily spend in USD",
    )
    max_concurrent_llm: int = Field(
        default=3,
        description="Maximum concurrent LLM requests",
    )


class RetryConfig(BaseSettings):
    """Retry policy configuration."""

    max_attempts: int = Field(
        default=3,
        description="Maximum retry attempts for failed tasks",
    )
    backoff_base_seconds: int = Field(
        default=5,
        description="Base delay for exponential backoff",
    )
    backoff_multiplier: int = Field(
        default=2,
        description="Multiplier for exponential backoff",
    )


class ShutdownConfig(BaseSettings):
    """Graceful shutdown configuration."""

    timeout_seconds: int = Field(
        default=30,
        description="Maximum time to wait for graceful shutdown",
    )
    drain_queue: bool = Field(
        default=False,
        description="Wait for all pending tasks to complete (not just current)",
    )


class ScheduleConfig(BaseSettings):
    """Scheduled task configuration (cron expressions)."""

    deduplication: str = Field(
        default="0 * * * *",
        description="Hourly deduplication",
    )
    obsolescence: str = Field(
        default="0 3 * * *",
        description="Daily obsolescence check at 3 AM",
    )
    gap_analysis: str = Field(
        default="0 5 * * 0",
        description="Weekly gap analysis on Sunday at 5 AM",
    )


class Settings(BaseSettings):
    """Main application settings.

    Configuration is loaded from:
    1. Environment variables (highest priority)
    2. YAML config file (if specified)
    3. Default values (lowest priority)
    """

    model_config = SettingsConfigDict(
        env_prefix="CURATOR_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    # API Configuration
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key (required for LLM features)",
    )

    @property
    def has_api_key(self) -> bool:
        """Check if API key is configured."""
        return bool(self.anthropic_api_key and self.anthropic_api_key.strip())

    # Service URLs
    knowledge_bridge_url: str = Field(
        default="http://localhost:4003",
        description="Knowledge Bridge service URL",
    )
    knowledge_store_url: str = Field(
        default="http://localhost:4004",
        description="Knowledge Store MCP server URL",
    )

    # Webhook Server
    webhook_host: str = Field(
        default="127.0.0.1",
        description="Webhook server bind host",
    )
    webhook_port: int = Field(
        default=4005,
        description="Webhook server port",
    )

    # Database
    db_path: Path = Field(
        default=Path("./curator.db"),
        description="Path to SQLite database",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )

    # Nested configurations
    models: ModelConfig = Field(default_factory=ModelConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    schedules: ScheduleConfig = Field(default_factory=ScheduleConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "Settings":
        """Load settings from YAML file, with env vars taking precedence."""
        if not path.exists():
            return cls()

        with open(path) as f:
            yaml_config = yaml.safe_load(f) or {}

        return cls(**cls._flatten_yaml(yaml_config))

    @classmethod
    def _flatten_yaml(cls, config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """Flatten nested YAML config for pydantic-settings."""
        result: dict[str, Any] = {}
        for key, value in config.items():
            full_key = f"{prefix}{key}" if prefix else key
            if isinstance(value, dict):
                result.update(cls._flatten_yaml(value, f"{full_key}__"))
            else:
                result[full_key] = value
        return result


# Global settings instance (lazy loaded)
_settings: Settings | None = None


def get_settings(config_path: Path | None = None) -> Settings:
    """Get application settings, loading from config file if specified.

    Args:
        config_path: Optional path to YAML config file.

    Returns:
        Application settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_yaml(config_path) if config_path else Settings()
    return _settings


def reset_settings() -> None:
    """Reset settings (useful for testing)."""
    global _settings
    _settings = None
