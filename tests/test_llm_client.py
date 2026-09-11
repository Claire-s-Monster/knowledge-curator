"""Tests for LLM client with Claude Agent SDK."""

from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

try:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
except ImportError:
    # SDK not available in CI.  Import the stub classes that client.py defines
    # so that isinstance() checks inside CuratorLLMClient.complete() resolve
    # correctly against the same class objects used to construct mock messages.
    from knowledge_curator.llm.client import (  # type: ignore[no-redef]
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )

from knowledge_curator.config import Settings, reset_settings
from knowledge_curator.llm.client import CuratorLLMClient, LLMResponse, LLMUsage


async def mock_query_generator(
    content: str = "Test response",
    cost: float = 0.001,
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "success",
) -> AsyncIterator[Any]:
    """Create a mock query generator that yields messages."""
    # Yield AssistantMessage with TextBlock (using real SDK types)
    yield AssistantMessage(
        content=[TextBlock(text=content)],
        model="claude-sonnet-4",
    )

    # Yield ResultMessage with usage (using real SDK type)
    yield ResultMessage(
        subtype=stop_reason,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        total_cost_usd=cost,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


def create_mock_query(
    content: str = "Test response",
    cost: float = 0.001,
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "success",
) -> AsyncMock:
    """Create a mock query function."""

    async def _mock(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for msg in mock_query_generator(
            content, cost, input_tokens, output_tokens, stop_reason
        ):
            yield msg

    return _mock


class TestLLMClientInitialization:
    """Tests for LLM client initialization."""

    def test_init_with_api_key(self) -> None:
        """Should initialize successfully with API key."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )

        client = CuratorLLMClient(settings)
        assert client is not None

    def test_init_without_api_key_warns_but_succeeds(self) -> None:
        """Should initialize with warning when API key is missing (SDK uses CLI)."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="",
            db_path=Path(":memory:"),
        )

        # SDK uses CLI credentials, so no ValueError raised
        client = CuratorLLMClient(settings)
        assert client is not None

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


class TestLLMClientModelSelection:
    """Tests for model selection logic."""

    def test_get_model_for_task_review(self) -> None:
        """Should return default model for review tasks."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        model = client.get_model_for_task("review")
        assert model == settings.models.default

    def test_get_model_for_task_feedback(self) -> None:
        """Should return simple model for feedback tasks."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        model = client.get_model_for_task("feedback")
        assert model == settings.models.simple_tasks

    def test_get_model_for_task_gaps(self) -> None:
        """Should return complex model for gaps tasks."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        model = client.get_model_for_task("gaps")
        assert model == settings.models.complex_tasks

    def test_get_model_for_unknown_task(self) -> None:
        """Should return default model for unknown task types."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        model = client.get_model_for_task("unknown_task")
        assert model == settings.models.default


class TestLLMClientComplete:
    """Tests for the complete() method."""

    @pytest.mark.asyncio
    async def test_complete_returns_response(self) -> None:
        """Should return LLMResponse with content and usage."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        mock_query = create_mock_query(
            content="Hello, world!",
            cost=0.002,
            input_tokens=50,
            output_tokens=25,
        )

        with patch("knowledge_curator.llm.client.query", mock_query):
            response = await client.complete("Say hello")

        assert isinstance(response, LLMResponse)
        assert response.content == "Hello, world!"
        assert response.cost_usd == 0.002
        assert response.input_tokens == 50
        assert response.output_tokens == 25

    @pytest.mark.asyncio
    async def test_complete_uses_task_model(self) -> None:
        """Should use appropriate model based on task type."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        captured_options = []

        async def capturing_query(
            *args: Any, **kwargs: Any
        ) -> AsyncIterator[Any]:
            captured_options.append(kwargs.get("options"))
            async for msg in mock_query_generator():
                yield msg

        with patch("knowledge_curator.llm.client.query", capturing_query):
            await client.complete("Test prompt", task_type="feedback")

        assert len(captured_options) == 1
        # Feedback uses simple model
        assert captured_options[0].model == settings.models.simple_tasks

    @pytest.mark.asyncio
    async def test_complete_with_system_prompt(self) -> None:
        """Should pass system prompt to SDK options."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        captured_options = []

        async def capturing_query(
            *args: Any, **kwargs: Any
        ) -> AsyncIterator[Any]:
            captured_options.append(kwargs.get("options"))
            async for msg in mock_query_generator():
                yield msg

        with patch("knowledge_curator.llm.client.query", capturing_query):
            await client.complete("Test prompt", system="You are helpful")

        assert len(captured_options) == 1
        assert captured_options[0].system_prompt == "You are helpful"


class TestLLMClientCompleteJSON:
    """Tests for the complete_json() method."""

    @pytest.mark.asyncio
    async def test_complete_json_parses_response(self) -> None:
        """Should parse JSON from response content."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        mock_query = create_mock_query(
            content='{"decision": "promote", "confidence": 0.95}',
            cost=0.003,
        )

        with patch("knowledge_curator.llm.client.query", mock_query):
            parsed, usage = await client.complete_json("Analyze this")

        assert parsed == {"decision": "promote", "confidence": 0.95}
        assert isinstance(usage, LLMUsage)
        assert usage.cost_usd == 0.003

    @pytest.mark.asyncio
    async def test_complete_json_handles_code_blocks(self) -> None:
        """Should strip markdown code blocks from JSON."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        mock_query = create_mock_query(
            content='```json\n{"result": "success"}\n```',
        )

        with patch("knowledge_curator.llm.client.query", mock_query):
            parsed, _ = await client.complete_json("Get result")

        assert parsed == {"result": "success"}

    @pytest.mark.asyncio
    async def test_complete_json_raises_on_invalid_json(self) -> None:
        """Should raise ValueError for invalid JSON."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        mock_query = create_mock_query(content="This is not JSON")

        with patch("knowledge_curator.llm.client.query", mock_query):
            with pytest.raises(ValueError, match="Invalid JSON response"):
                await client.complete_json("Get JSON")


class TestLLMClientUsageStats:
    """Tests for usage statistics methods."""

    def test_get_usage_stats(self) -> None:
        """Should return usage stats from rate limiter."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        stats = client.get_usage_stats()
        assert isinstance(stats, dict)
        assert "total_cost_usd" in stats

    def test_is_budget_exceeded_initially_false(self) -> None:
        """Should return False when budget not exceeded."""
        reset_settings()
        settings = Settings(
            anthropic_api_key="sk-ant-test-key",
            db_path=Path(":memory:"),
        )
        client = CuratorLLMClient(settings)

        assert client.is_budget_exceeded() is False
