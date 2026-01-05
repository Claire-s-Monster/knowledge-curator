"""Pytest fixtures for Knowledge Curator tests."""

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import pytest

from knowledge_curator.config import Settings, reset_settings
from knowledge_curator.database.repository import Repository


@pytest.fixture
def settings() -> Settings:
    """Create test settings with in-memory database."""
    reset_settings()
    return Settings(
        db_path=Path(":memory:"),
        log_level="DEBUG",
        anthropic_api_key="test-key",
    )


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncGenerator[Repository, None]:
    """Create a test repository with temporary database."""
    db_path = tmp_path / "test_curator.db"
    repo = Repository(db_path)
    await repo.connect()
    yield repo
    await repo.close()


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
