"""Anthropic API client wrapper for Knowledge Curator.

Provides a high-level interface for LLM interactions with:
- Automatic rate limiting
- Cost tracking
- Model selection per task type
- Retry handling
- Structured output parsing
"""

import json
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic, APIError, RateLimitError
from anthropic.types import MessageParam, TextBlock
from loguru import logger

from knowledge_curator.config import Settings
from knowledge_curator.llm.rate_limiter import RateLimiter


@dataclass
class LLMResponse:
    """Response from LLM request."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str


@dataclass
class LLMUsage:
    """Token usage statistics."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float


class CuratorLLMClient:
    """Anthropic client wrapper with rate limiting and cost tracking.

    Features:
    - Automatic model selection based on task type
    - Rate limiting with token bucket algorithm
    - Daily budget enforcement
    - Cost tracking per request
    - Retry handling for transient errors
    """

    # Task type to model mapping
    TASK_MODELS = {
        "review": "default",  # Use default model (Sonnet)
        "feedback": "simple",  # Use simple model (Haiku)
        "dedup": "default",
        "obsolescence": "default",
        "gaps": "complex",  # Use complex model (Sonnet/Opus)
        "merge": "default",
    }

    def __init__(self, settings: Settings) -> None:
        """Initialize LLM client.

        Args:
            settings: Application settings.
        """
        self.settings = settings

        # Initialize Anthropic client
        self._client = Anthropic(api_key=settings.anthropic_api_key)

        # Initialize rate limiter
        self._rate_limiter = RateLimiter(
            daily_budget_usd=settings.rate_limits.daily_budget_usd,
            max_concurrent=settings.rate_limits.max_concurrent_llm,
        )

        # Model aliases
        self._models = {
            "default": settings.models.default,
            "simple": settings.models.simple_tasks,
            "complex": settings.models.complex_tasks,
        }

        logger.info(
            f"LLM client initialized: default={self._models['default']}, "
            f"budget=${settings.rate_limits.daily_budget_usd}/day"
        )

    def get_model_for_task(self, task_type: str) -> str:
        """Get the appropriate model for a task type.

        Args:
            task_type: Type of task (review, feedback, etc.)

        Returns:
            Model identifier.
        """
        model_alias = self.TASK_MODELS.get(task_type, "default")
        return self._models.get(model_alias, self._models["default"])

    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        task_type: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a completion request to the API.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            model: Model to use (overrides task_type selection).
            task_type: Task type for automatic model selection.
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature (0 = deterministic).

        Returns:
            LLMResponse with content and usage.

        Raises:
            ValueError: If budget exceeded.
            APIError: If API request fails.
        """
        # Select model
        if model is None:
            model = self.get_model_for_task(task_type or "default")

        # Estimate tokens for rate limiting
        estimated_tokens = len(prompt) // 4 + max_tokens

        # Acquire rate limit slot
        if not await self._rate_limiter.acquire(model, estimated_tokens):
            raise ValueError("Daily budget exceeded")

        try:
            # Build messages
            messages: list[MessageParam] = [{"role": "user", "content": prompt}]

            # Make API request
            logger.debug(
                f"Sending request to {model}, estimated {estimated_tokens} tokens"
            )

            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or "",
                messages=messages,
            )

            # Extract usage
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            # Record usage and get cost
            cost = await self._rate_limiter.record_usage(
                model, input_tokens, output_tokens
            )

            # Extract content
            content = ""
            if response.content:
                first_block = response.content[0]
                if isinstance(first_block, TextBlock):
                    content = first_block.text

            return LLMResponse(
                content=content,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                stop_reason=response.stop_reason or "unknown",
            )

        except RateLimitError as e:
            logger.warning(f"Rate limited by API: {e}")
            raise

        except APIError as e:
            logger.error(f"API error: {e}")
            raise

        finally:
            self._rate_limiter.release()

    async def complete_json(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        task_type: str | None = None,
        max_tokens: int = 4096,
    ) -> tuple[dict[str, Any], LLMUsage]:
        """Send a completion request expecting JSON response.

        Args:
            prompt: User prompt (should request JSON output).
            system: Optional system prompt.
            model: Model to use.
            task_type: Task type for model selection.
            max_tokens: Maximum output tokens.

        Returns:
            Tuple of (parsed JSON dict, usage stats).

        Raises:
            ValueError: If response is not valid JSON.
        """
        # Add JSON instruction to system prompt
        json_system = (system or "") + (
            "\n\nYou must respond with valid JSON only. "
            "Do not include any text before or after the JSON."
        )

        response = await self.complete(
            prompt=prompt,
            system=json_system,
            model=model,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=0.0,  # Deterministic for JSON
        )

        # Parse JSON from response
        content = response.content.strip()

        # Handle markdown code blocks
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first and last lines (```json and ```)
            content = "\n".join(lines[1:-1])

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(
                f"Failed to parse JSON response: {e}\nContent: {content[:500]}"
            )
            raise ValueError(f"Invalid JSON response: {e}") from e

        usage = LLMUsage(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.input_tokens + response.output_tokens,
            cost_usd=response.cost_usd,
        )

        return parsed, usage

    def get_usage_stats(self) -> dict[str, object]:
        """Get current usage statistics.

        Returns:
            Dictionary with usage stats.
        """
        return self._rate_limiter.get_daily_stats()

    def is_budget_exceeded(self) -> bool:
        """Check if daily budget is exceeded.

        Returns:
            True if budget exceeded.
        """
        return self._rate_limiter.is_budget_exceeded()
