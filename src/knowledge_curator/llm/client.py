"""Claude Agent SDK client wrapper for Knowledge Curator.

Provides a high-level interface for LLM interactions with:
- Claude Agent SDK integration (query function)
- Rate limiting for internal tracking
- Cost tracking from SDK ResultMessage
- Model selection per task type
- Structured output parsing
"""

import json
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ProcessError,
    ResultMessage,
    TextBlock,
    query,
)
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
    """Claude Agent SDK client wrapper with rate limiting and cost tracking.

    Features:
    - Automatic model selection based on task type
    - Rate limiting with token bucket algorithm
    - Daily budget enforcement
    - Cost tracking from SDK ResultMessage
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

        Note:
            The Claude Agent SDK uses Claude Code CLI credentials.
            The API key in settings is optional but logged for awareness.
        """
        self.settings = settings

        # SDK uses Claude Code CLI credentials
        if not settings.has_api_key:
            logger.warning(
                "No API key in config; SDK will use Claude Code CLI credentials"
            )

        # Initialize rate limiter for internal tracking
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
            f"LLM client initialized with SDK: default={self._models['default']}, "
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
        """Send a completion request using Claude Agent SDK.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            model: Model to use (overrides task_type selection).
            task_type: Task type for automatic model selection.
            max_tokens: Maximum output tokens (used for rate limit estimation).
            temperature: Sampling temperature (not directly supported by SDK).

        Returns:
            LLMResponse with content and usage.

        Raises:
            ValueError: If budget exceeded.
            ClaudeSDKError: If SDK request fails.
            ProcessError: If CLI process fails.
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
            # Build SDK options
            # CLI path for systemd service (not in default PATH)
            cli_path = "/home/memento/.conda/envs/ClaudeCode/bin/claude"

            # Capture stderr for debugging subprocess failures
            stderr_lines: list[str] = []

            def capture_stderr(line: str) -> None:
                stderr_lines.append(line)
                logger.debug(f"SDK stderr: {line}")

            options = ClaudeAgentOptions(
                model=model,
                system_prompt=system,
                max_turns=1,  # Single completion
                cli_path=cli_path,
                stderr=capture_stderr,
            )

            logger.debug(
                f"Sending SDK request to {model}, estimated {estimated_tokens} tokens"
            )

            # Collect response
            content = ""
            input_tokens = 0
            output_tokens = 0
            cost_usd = 0.0
            stop_reason = "unknown"

            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            content += block.text

                elif isinstance(message, ResultMessage):
                    cost_usd = message.total_cost_usd or 0.0
                    stop_reason = message.subtype
                    if message.usage:
                        input_tokens = message.usage.get("input_tokens", 0)
                        output_tokens = message.usage.get("output_tokens", 0)

            # Record usage with rate limiter
            await self._rate_limiter.record_usage(model, input_tokens, output_tokens)

            logger.debug(
                f"SDK response: {len(content)} chars, "
                f"{input_tokens}+{output_tokens} tokens, ${cost_usd:.4f}"
            )

            return LLMResponse(
                content=content,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                stop_reason=stop_reason,
            )

        except ClaudeSDKError as e:
            if stderr_lines:
                logger.error(f"SDK error: {e}\nStderr:\n" + "\n".join(stderr_lines))
            else:
                logger.error(f"SDK error: {e}")
            raise

        except ProcessError as e:
            if stderr_lines:
                logger.error(
                    f"Process error: {e}\nStderr:\n" + "\n".join(stderr_lines)
                )
            else:
                logger.error(f"Process error: {e}")
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
