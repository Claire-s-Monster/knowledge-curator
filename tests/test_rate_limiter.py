"""Tests for rate limiter."""

import asyncio
import time
from pathlib import Path

import pytest

from knowledge_curator.database.repository import Repository
from knowledge_curator.llm.rate_limiter import (
    FALLBACK_MODEL_COST,
    MODEL_COSTS,
    DailyCost,
    ModelLimits,
    RateLimiter,
    TokenBucket,
)


class TestTokenBucket:
    """Tests for TokenBucket class."""

    def test_initial_full(self) -> None:
        """Test bucket starts full."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.tokens == 10

    def test_consume_success(self) -> None:
        """Test successful consumption."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(5) is True
        assert bucket.tokens == 5

    def test_consume_insufficient(self) -> None:
        """Test consumption fails when insufficient tokens."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        bucket.consume(8)
        assert bucket.consume(5) is False

    def test_refill(self) -> None:
        """Test token refill over time."""
        bucket = TokenBucket(capacity=10, refill_rate=10.0)  # 10 tokens/sec
        bucket.consume(10)  # Empty the bucket

        # Wait for some refill
        time.sleep(0.1)
        bucket.refill()

        # Should have refilled about 1 token (10 * 0.1)
        assert bucket.tokens >= 0.5

    def test_wait_time(self) -> None:
        """Test wait time calculation."""
        bucket = TokenBucket(capacity=10, refill_rate=10.0)  # 10 tokens/sec
        bucket.consume(10)  # Empty the bucket

        # Need 5 tokens, refill rate is 10/sec, so wait ~0.5s
        wait = bucket.wait_time(5)
        assert 0.4 <= wait <= 0.6

    def test_no_wait_when_available(self) -> None:
        """Test no wait when tokens available."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.wait_time(5) == 0.0


class TestRateLimiter:
    """Tests for RateLimiter class."""

    @pytest.fixture
    def limiter(self) -> RateLimiter:
        """Create a rate limiter for testing."""
        return RateLimiter(
            daily_budget_usd=10.0,
            max_concurrent=2,
            model_limits={
                "test-model": ModelLimits(
                    requests_per_minute=60,
                    tokens_per_minute=10000,
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_acquire_success(self, limiter: RateLimiter) -> None:
        """Test successful acquire."""
        result = await limiter.acquire("test-model", 100)
        assert result is True
        limiter.release()

    @pytest.mark.asyncio
    async def test_acquire_budget_exceeded(self, limiter: RateLimiter) -> None:
        """Test acquire fails when budget exceeded."""
        # Simulate high cost usage
        await limiter.record_usage("test-model", 1_000_000, 1_000_000)

        result = await limiter.acquire("test-model", 100)
        assert result is False

    @pytest.mark.asyncio
    async def test_concurrent_limit(self, limiter: RateLimiter) -> None:
        """Test concurrent request limiting."""
        # Acquire two slots (the max)
        await limiter.acquire("test-model", 100)
        await limiter.acquire("test-model", 100)

        # Third acquire should block (we'll timeout)
        async def try_acquire():
            return await asyncio.wait_for(
                limiter.acquire("test-model", 100),
                timeout=0.1,
            )

        with pytest.raises(asyncio.TimeoutError):
            await try_acquire()

        # Release one slot
        limiter.release()

        # Now should succeed
        result = await limiter.acquire("test-model", 100)
        assert result is True

    @pytest.mark.asyncio
    async def test_record_usage(self, limiter: RateLimiter) -> None:
        """Test usage recording."""
        cost = await limiter.record_usage("test-model", 1000, 100)

        assert cost > 0
        stats = limiter.get_daily_stats()
        assert stats["requests"] == 1
        assert stats["input_tokens"] == 1000
        assert stats["output_tokens"] == 100

    def test_estimate_cost(self, limiter: RateLimiter) -> None:
        """Test cost estimation."""
        # Sonnet pricing: $3/MTok input, $15/MTok output
        cost = limiter.estimate_cost(
            "claude-sonnet-4-20250514",
            input_tokens=1_000_000,
            output_tokens=100_000,
        )

        expected = 3.0 + 1.5  # $3 input + $1.5 output
        assert cost == pytest.approx(expected)

    def test_fallback_model_cost_dominates_known_costs(self) -> None:
        """Test FALLBACK_MODEL_COST is >= every MODEL_COSTS entry, component-wise.

        FALLBACK_MODEL_COST is deliberately the most expensive known tier so
        that an unknown model over-estimates against the daily budget ceiling
        rather than silently under-counting it. This asserts the relationship
        (fallback >= every known cost), never the literal tuple, so the test
        survives a price update but fails if someone reintroduces a cheap
        default.
        """
        for model, (in_cost, out_cost) in MODEL_COSTS.items():
            assert FALLBACK_MODEL_COST[0] >= in_cost, (
                f"fallback input cost must dominate {model}'s input cost"
            )
            assert FALLBACK_MODEL_COST[1] >= out_cost, (
                f"fallback output cost must dominate {model}'s output cost"
            )

    def test_estimate_cost_unknown_model_over_estimates(
        self, limiter: RateLimiter
    ) -> None:
        """Test an unknown model's estimated cost is >= every known model's.

        Unknown models must err toward over-estimating so the daily budget
        ceiling can never be silently overrun. What's under test is the
        direction of the error (unknown >= known), not its magnitude.
        """
        unknown_cost = limiter.estimate_cost(
            "definitely-not-a-real-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        for model in MODEL_COSTS:
            known_cost = limiter.estimate_cost(
                model,
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
            assert unknown_cost >= known_cost, (
                f"unknown model cost must dominate known model {model}'s cost"
            )

    def test_get_daily_stats(self, limiter: RateLimiter) -> None:
        """Test daily stats retrieval."""
        stats = limiter.get_daily_stats()

        assert "date" in stats
        assert stats["budget_usd"] == 10.0
        assert stats["budget_remaining_usd"] == 10.0
        assert stats["requests"] == 0

    @pytest.mark.asyncio
    async def test_budget_tracking(self, limiter: RateLimiter) -> None:
        """Test budget tracking across requests."""
        # Record some usage
        await limiter.record_usage("claude-sonnet-4-20250514", 100000, 10000)
        await limiter.record_usage("claude-sonnet-4-20250514", 100000, 10000)

        stats = limiter.get_daily_stats()
        assert stats["requests"] == 2
        assert stats["budget_remaining_usd"] < 10.0
        assert stats["budget_used_percent"] > 0


class TestDailyCost:
    """Tests for DailyCost dataclass."""

    def test_default_values(self) -> None:
        """Test default values."""
        cost = DailyCost(date="2026-01-04")

        assert cost.date == "2026-01-04"
        assert cost.total_cost_usd == 0.0
        assert cost.requests == 0
        assert cost.input_tokens == 0
        assert cost.output_tokens == 0


class TestRateLimiterPersistence:
    """Tests for RateLimiter <-> Repository cost persistence (issue #7).

    These tests exercise the production `record_usage` / `load_daily_cost`
    path against a real Repository, rather than asserting against the
    repository's writer methods directly, so they actually pin the
    daemon-restart bug: pre-fix, a freshly constructed RateLimiter always
    started with `DailyCost(total_cost_usd=0.0)` regardless of what had
    already been persisted.
    """

    @pytest.mark.asyncio
    async def test_daily_cost_survives_restart(self, tmp_path: Path) -> None:
        """Cost from one RateLimiter must be visible to a second, independent
        RateLimiter/Repository pair pointed at the same database file.

        In-memory-vs-file choice: uses a file-backed sqlite database
        (not ':memory:') on purpose. An in-memory database only exists for
        the lifetime of a single connection, so two independent Repository
        objects each opening their own ':memory:' connection would see two
        separate empty databases -- this test would pass even without the
        fix, testing nothing. A shared file path forces the second
        Repository's connection to actually read back what the first one
        wrote, which is what "survives a daemon restart" means in practice.
        """
        db_path = tmp_path / "restart_test.db"

        repo1 = Repository(db_path)
        await repo1.connect()
        limiter1 = RateLimiter(daily_budget_usd=10.0, repository=repo1)
        cost = await limiter1.record_usage(
            "claude-sonnet-4-20250514", 100_000, 10_000
        )
        await repo1.close()

        repo2 = Repository(db_path)
        await repo2.connect()
        limiter2 = RateLimiter(daily_budget_usd=10.0, repository=repo2)
        await limiter2.load_daily_cost()

        # Pre-fix: limiter2._daily_cost was a freshly constructed
        # DailyCost(total_cost_usd=0.0) because load_daily_cost() as a
        # concept did not exist / was never called, so this would have
        # compared 0.0 against the real accrued cost (> 0) and failed.
        assert limiter2.get_daily_stats()["total_cost_usd"] == pytest.approx(cost)

        await repo2.close()

    @pytest.mark.asyncio
    async def test_record_usage_persists_delta_not_running_total(
        self, tmp_path: Path
    ) -> None:
        """Two record_usage calls must upsert as one accumulating row, not
        double-count via a running total passed to the accumulating upsert.
        """
        db_path = tmp_path / "delta_test.db"
        repo = Repository(db_path)
        await repo.connect()
        limiter = RateLimiter(daily_budget_usd=10.0, repository=repo)

        await limiter.record_usage("claude-sonnet-4-20250514", 1000, 100)
        await limiter.record_usage("claude-sonnet-4-20250514", 2000, 200)

        cursor = await repo.conn.execute(
            "SELECT task_count, tokens_input, tokens_output FROM cost_log"
        )
        rows = await cursor.fetchall()

        assert len(rows) == 1
        assert rows[0]["task_count"] == 2
        assert rows[0]["tokens_input"] == 3000
        assert rows[0]["tokens_output"] == 300

        await repo.close()

    @pytest.mark.asyncio
    async def test_budget_enforced_against_restored_cost(
        self, tmp_path: Path
    ) -> None:
        """The daily budget ceiling must hold after a simulated restart.

        This is the actual security property behind issue #7: a restarted
        daemon that forgot today's spend would let a caller burn through
        the budget a second time.
        """
        db_path = tmp_path / "budget_test.db"

        repo1 = Repository(db_path)
        await repo1.connect()
        limiter1 = RateLimiter(daily_budget_usd=0.01, repository=repo1)
        # Sonnet pricing ($3/$15 per MTok): 10k in + 1k out = $0.045.
        await limiter1.record_usage("claude-sonnet-4-20250514", 10_000, 1_000)
        await repo1.close()

        repo2 = Repository(db_path)
        await repo2.connect()
        limiter2 = RateLimiter(daily_budget_usd=0.01, repository=repo2)
        await limiter2.load_daily_cost()

        assert limiter2.is_budget_exceeded() is True

        await repo2.close()

    @pytest.mark.asyncio
    async def test_rate_limiter_without_repository_still_works(self) -> None:
        """No repository configured: usage recording and cost loading must
        not raise, and in-memory tracking must still function.
        """
        limiter = RateLimiter(daily_budget_usd=10.0)

        cost = await limiter.record_usage("claude-sonnet-4-20250514", 1000, 100)
        await limiter.load_daily_cost()

        assert cost > 0
        stats = limiter.get_daily_stats()
        assert stats["requests"] == 1
        assert stats["input_tokens"] == 1000
        assert stats["output_tokens"] == 100
