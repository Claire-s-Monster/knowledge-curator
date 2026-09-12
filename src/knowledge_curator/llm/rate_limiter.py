"""Token bucket rate limiter for Anthropic API.

Implements rate limiting to stay within API limits and budget constraints.
Supports per-model limits and daily budget tracking.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from knowledge_curator.database.repository import Repository


@dataclass
class ModelLimits:
    """Rate limits for a specific model."""

    requests_per_minute: int
    tokens_per_minute: int


# Default rate limits per model (conservative estimates)
DEFAULT_MODEL_LIMITS: dict[str, ModelLimits] = {
    "sonnet": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=40000,
    ),
    "claude-sonnet-5": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=40000,
    ),
    "haiku": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=50000,
    ),
    "claude-haiku-4-5": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=50000,
    ),
    "opus": ModelLimits(
        requests_per_minute=20,
        tokens_per_minute=20000,
    ),
    "claude-opus-5": ModelLimits(
        requests_per_minute=20,
        tokens_per_minute=20000,
    ),
    # Legacy dated IDs, kept for back-compat
    "claude-sonnet-4-20250514": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=40000,
    ),
    "claude-3-5-haiku-20241022": ModelLimits(
        requests_per_minute=50,
        tokens_per_minute=50000,
    ),
    "claude-opus-4-20250514": ModelLimits(
        requests_per_minute=20,
        tokens_per_minute=20000,
    ),
}

# Anthropic first-party API rates, verified 2026-09-11. Bare aliases
# ("sonnet"/"haiku"/"opus") resolve to the latest model in each tier, so
# their pricing entries must be re-checked whenever a tier's underlying
# model changes.
MODEL_COSTS: dict[str, tuple[float, float]] = {
    # (input_cost_per_mtok, output_cost_per_mtok)
    "opus": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "haiku": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Legacy dated IDs, kept for back-compat
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (0.25, 1.25),
    "claude-opus-4-20250514": (15.0, 75.0),
}

# Component-wise most expensive known tier, derived from MODEL_COSTS rather
# than hardcoded so it cannot fall behind as prices/entries change. An
# unknown model therefore over-estimates rather than under-estimates, so the
# daily budget ceiling can never be silently overrun. Trade-off, stated
# plainly: legacy entries are retained for back-compat and include a
# previous-generation Opus at (15.0, 75.0), so an unknown CURRENT-generation
# model over-estimates by roughly 3x. That is deliberate -- estimate_cost's
# warning names the unknown model, so the correct response is to add it to
# MODEL_COSTS rather than to lower this fallback.
FALLBACK_MODEL_COST: tuple[float, float] = (
    max(c[0] for c in MODEL_COSTS.values()),
    max(c[1] for c in MODEL_COSTS.values()),
)


@dataclass
class TokenBucket:
    """Token bucket for rate limiting.

    Implements the token bucket algorithm where tokens are added
    at a fixed rate up to a maximum capacity.
    """

    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        """Initialize with full bucket."""
        self.tokens = self.capacity

    def refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, amount: float) -> bool:
        """Try to consume tokens from the bucket.

        Args:
            amount: Number of tokens to consume.

        Returns:
            True if tokens were consumed, False if insufficient.
        """
        self.refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_time(self, amount: float) -> float:
        """Calculate wait time until tokens are available.

        Args:
            amount: Number of tokens needed.

        Returns:
            Seconds to wait (0 if tokens available now).
        """
        self.refill()
        if self.tokens >= amount:
            return 0.0
        deficit = amount - self.tokens
        return deficit / self.refill_rate


@dataclass
class DailyCost:
    """Track daily API costs."""

    date: str
    total_cost_usd: float = 0.0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class RateLimiter:
    """Rate limiter for Anthropic API with budget controls.

    Features:
    - Per-model request and token rate limits
    - Daily budget enforcement
    - Concurrent request limiting
    - Cost tracking
    """

    def __init__(
        self,
        daily_budget_usd: float = 10.0,
        max_concurrent: int = 3,
        model_limits: dict[str, ModelLimits] | None = None,
        repository: "Repository | None" = None,
    ) -> None:
        """Initialize rate limiter.

        Args:
            daily_budget_usd: Maximum daily spend in USD.
            max_concurrent: Maximum concurrent LLM requests.
            model_limits: Custom rate limits per model.
            repository: Optional repository used to persist and restore the
                daily spend total across daemon restarts.
        """
        self.daily_budget_usd = daily_budget_usd
        self.max_concurrent = max_concurrent
        self.model_limits = model_limits or DEFAULT_MODEL_LIMITS
        self._repository = repository

        # Semaphore for concurrent request limiting
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Token buckets per model (for requests)
        self._request_buckets: dict[str, TokenBucket] = {}

        # Token buckets per model (for API tokens)
        self._token_buckets: dict[str, TokenBucket] = {}

        # Daily cost tracking
        self._daily_cost: DailyCost | None = None

        # Lock for thread-safe updates
        self._lock = asyncio.Lock()

    def _get_request_bucket(self, model: str) -> TokenBucket:
        """Get or create request bucket for model."""
        if model not in self._request_buckets:
            if model not in self.model_limits:
                logger.warning(
                    f"Unknown model '{model}' not in model_limits; "
                    "using conservative default request/token limits"
                )
            limits = self.model_limits.get(
                model,
                ModelLimits(requests_per_minute=20, tokens_per_minute=20000),
            )
            self._request_buckets[model] = TokenBucket(
                capacity=limits.requests_per_minute,
                refill_rate=limits.requests_per_minute / 60.0,
            )
        return self._request_buckets[model]

    def _get_token_bucket(self, model: str) -> TokenBucket:
        """Get or create token bucket for model."""
        if model not in self._token_buckets:
            if model not in self.model_limits:
                logger.warning(
                    f"Unknown model '{model}' not in model_limits; "
                    "using conservative default request/token limits"
                )
            limits = self.model_limits.get(
                model,
                ModelLimits(requests_per_minute=20, tokens_per_minute=20000),
            )
            self._token_buckets[model] = TokenBucket(
                capacity=limits.tokens_per_minute,
                refill_rate=limits.tokens_per_minute / 60.0,
            )
        return self._token_buckets[model]

    def _get_daily_cost(self) -> DailyCost:
        """Get or create daily cost tracker."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._daily_cost is None or self._daily_cost.date != today:
            self._daily_cost = DailyCost(date=today)
        return self._daily_cost

    async def load_daily_cost(self) -> None:
        """Restore today's accumulated spend from the repository.

        Daemon restarts previously reset the in-memory daily total to zero,
        silently discarding the budget already spent earlier the same day.
        This restores that total from the persisted cost log so the budget
        ceiling in `acquire`/`is_budget_exceeded` reflects reality.

        A failed restore is logged and otherwise ignored so a database
        hiccup never prevents the daemon from starting.
        """
        if self._repository is None:
            logger.debug("No repository configured; skipping daily cost restore")
            return

        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            restored_total = await self._repository.get_daily_cost(today)
        except Exception as e:
            logger.warning(f"Failed to restore daily cost from repository: {e}")
            return

        async with self._lock:
            self._daily_cost = DailyCost(date=today, total_cost_usd=restored_total)

        if restored_total > 0:
            logger.info(
                f"Restored daily cost from repository: ${restored_total:.4f} for {today}"
            )

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost for a request.

        Args:
            model: Model name.
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Estimated cost in USD.
        """
        costs = MODEL_COSTS.get(model)
        if costs is None:
            logger.warning(
                f"Unknown model '{model}' not in MODEL_COSTS; "
                "using most-expensive-tier fallback cost estimate"
            )
            costs = FALLBACK_MODEL_COST
        input_cost = (input_tokens / 1_000_000) * costs[0]
        output_cost = (output_tokens / 1_000_000) * costs[1]
        return input_cost + output_cost

    async def acquire(
        self,
        model: str,
        estimated_tokens: int = 4000,
    ) -> bool:
        """Acquire permission to make an API request.

        Args:
            model: Model to use.
            estimated_tokens: Estimated total tokens for request.

        Returns:
            True if acquired, False if budget exceeded.

        This method will wait if rate limited, but return False
        immediately if daily budget is exceeded.
        """
        async with self._lock:
            # Check daily budget
            daily = self._get_daily_cost()
            if daily.total_cost_usd >= self.daily_budget_usd:
                logger.warning(
                    f"Daily budget exceeded: ${daily.total_cost_usd:.2f} >= "
                    f"${self.daily_budget_usd:.2f}"
                )
                return False

            # Get buckets
            request_bucket = self._get_request_bucket(model)
            token_bucket = self._get_token_bucket(model)

            # Wait for request bucket if needed
            wait_time = request_bucket.wait_time(1)
            if wait_time > 0:
                logger.debug(f"Rate limited (requests), waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

            # Wait for token bucket if needed
            wait_time = token_bucket.wait_time(estimated_tokens)
            if wait_time > 0:
                logger.debug(f"Rate limited (tokens), waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

            # Consume from buckets
            request_bucket.consume(1)
            token_bucket.consume(estimated_tokens)

        # Acquire semaphore (wait for concurrent slot)
        await self._semaphore.acquire()
        return True

    def release(self) -> None:
        """Release the concurrent request slot."""
        self._semaphore.release()

    async def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Record API usage and return cost.

        Args:
            model: Model used.
            input_tokens: Actual input tokens.
            output_tokens: Actual output tokens.

        Returns:
            Cost in USD.
        """
        cost = self.estimate_cost(model, input_tokens, output_tokens)

        async with self._lock:
            daily = self._get_daily_cost()
            daily.total_cost_usd += cost
            daily.requests += 1
            daily.input_tokens += input_tokens
            daily.output_tokens += output_tokens
            record_date = daily.date
            daily_total = daily.total_cost_usd

        logger.debug(
            f"Recorded usage: model={model}, in={input_tokens}, out={output_tokens}, "
            f"cost=${cost:.4f}, daily_total=${daily_total:.2f}"
        )

        # Persist this call's delta outside the lock so concurrent LLM calls
        # do not serialise behind disk I/O. Repository.record_cost is an
        # accumulating upsert, so only this call's delta is passed here.
        if self._repository is not None:
            try:
                await self._repository.record_cost(
                    date=record_date,
                    model=model,
                    tokens_input=input_tokens,
                    tokens_output=output_tokens,
                    estimated_cost_usd=cost,
                )
            except Exception as e:
                logger.warning(f"Failed to persist cost record: {e}")

        return cost

    def get_daily_stats(self) -> dict[str, object]:
        """Get current daily usage statistics.

        Returns:
            Dictionary with usage stats.
        """
        daily = self._get_daily_cost()
        return {
            "date": daily.date,
            "total_cost_usd": daily.total_cost_usd,
            "budget_usd": self.daily_budget_usd,
            "budget_remaining_usd": max(
                0, self.daily_budget_usd - daily.total_cost_usd
            ),
            "budget_used_percent": (daily.total_cost_usd / self.daily_budget_usd) * 100,
            "requests": daily.requests,
            "input_tokens": daily.input_tokens,
            "output_tokens": daily.output_tokens,
        }

    def is_budget_exceeded(self) -> bool:
        """Check if daily budget is exceeded.

        Returns:
            True if budget exceeded.
        """
        daily = self._get_daily_cost()
        return daily.total_cost_usd >= self.daily_budget_usd
