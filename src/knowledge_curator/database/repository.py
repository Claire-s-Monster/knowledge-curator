"""SQLite repository for task queue and decision logging.

Provides persistent storage for:
- Task queue (survives daemon restarts)
- Dead letter queue (failed tasks)
- Decision log (audit trail)
- Cost tracking
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from knowledge_curator.core.models import (
    DeadLetterEntry,
    QueuedTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)

# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Task queue for persistence
CREATE TABLE IF NOT EXISTS task_queue (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    priority INTEGER DEFAULT 2,
    payload JSON NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);

-- Dead letter queue for failed tasks
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    original_task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    payload JSON NOT NULL,
    error_message TEXT,
    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Decision log for auditing
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    entry_id TEXT,
    decision TEXT NOT NULL,
    confidence REAL,
    reason TEXT,
    model_used TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cost tracking
CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    model TEXT NOT NULL,
    tokens_input INTEGER NOT NULL,
    tokens_output INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    task_count INTEGER NOT NULL,
    UNIQUE(date, model)
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_type ON task_queue(task_type);
CREATE INDEX IF NOT EXISTS idx_decision_entry ON decision_log(entry_id);
CREATE INDEX IF NOT EXISTS idx_decision_task ON decision_log(task_id);
CREATE INDEX IF NOT EXISTS idx_cost_date ON cost_log(date);
"""


class Repository:
    """Async SQLite repository for curator data."""

    def __init__(self, db_path: Path) -> None:
        """Initialize repository with database path.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._init_schema()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _init_schema(self) -> None:
        """Initialize database schema if needed."""
        if not self._connection:
            raise RuntimeError("Database not connected")

        await self._connection.executescript(SCHEMA_SQL)

        # Check/set schema version
        cursor = await self._connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        )
        row = await cursor.fetchone()

        if row is None:
            await self._connection.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

        await self._connection.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get active database connection."""
        if not self._connection:
            raise RuntimeError("Database not connected")
        return self._connection

    # -------------------------------------------------------------------------
    # Task Queue Operations
    # -------------------------------------------------------------------------

    async def enqueue_task(self, task: QueuedTask) -> None:
        """Add a task to the queue.

        Args:
            task: Task to enqueue.
        """
        await self.conn.execute(
            """
            INSERT INTO task_queue
                (id, task_type, priority, payload, status, created_at, retry_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.task_type.value,
                task.priority.value,
                json.dumps(task.payload),
                task.status.value,
                task.created_at.isoformat(),
                task.retry_count,
            ),
        )
        await self.conn.commit()

    async def get_next_task(self) -> QueuedTask | None:
        """Get the next pending task by priority and creation time.

        Returns:
            Next task to process, or None if queue is empty.
        """
        cursor = await self.conn.execute(
            """
            SELECT * FROM task_queue
            WHERE status = 'pending'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_task(row)

    async def get_task(self, task_id: str) -> QueuedTask | None:
        """Get a specific task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            Task if found, None otherwise.
        """
        cursor = await self.conn.execute(
            "SELECT * FROM task_queue WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_task(row)

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: str | None = None,
    ) -> None:
        """Update task status.

        Args:
            task_id: Task identifier.
            status: New status.
            error_message: Error message if failed.
        """
        now = datetime.now(UTC).isoformat()

        if status == TaskStatus.PROCESSING:
            await self.conn.execute(
                "UPDATE task_queue SET status = ?, started_at = ? WHERE id = ?",
                (status.value, now, task_id),
            )
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            await self.conn.execute(
                """
                UPDATE task_queue
                SET status = ?, completed_at = ?, error_message = ?
                WHERE id = ?
                """,
                (status.value, now, error_message, task_id),
            )
        else:
            await self.conn.execute(
                "UPDATE task_queue SET status = ? WHERE id = ?",
                (status.value, task_id),
            )

        await self.conn.commit()

    async def increment_retry(self, task_id: str) -> int:
        """Increment retry count for a task.

        Args:
            task_id: Task identifier.

        Returns:
            New retry count.
        """
        await self.conn.execute(
            """
            UPDATE task_queue
            SET retry_count = retry_count + 1, status = 'pending'
            WHERE id = ?
            """,
            (task_id,),
        )
        await self.conn.commit()

        cursor = await self.conn.execute(
            "SELECT retry_count FROM task_queue WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return row["retry_count"] if row else 0

    async def get_pending_count(self) -> int:
        """Get count of pending tasks.

        Returns:
            Number of pending tasks.
        """
        cursor = await self.conn.execute(
            "SELECT COUNT(*) as count FROM task_queue WHERE status = 'pending'"
        )
        row = await cursor.fetchone()
        return row["count"] if row else 0

    async def get_queue_stats(self) -> dict[str, int]:
        """Get queue statistics by status.

        Returns:
            Dictionary of status -> count.
        """
        cursor = await self.conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM task_queue
            GROUP BY status
            """
        )
        rows = await cursor.fetchall()
        return {row["status"]: row["count"] for row in rows}

    # -------------------------------------------------------------------------
    # Dead Letter Queue Operations
    # -------------------------------------------------------------------------

    async def move_to_dlq(self, task: QueuedTask, error_message: str) -> None:
        """Move a failed task to the dead letter queue.

        Args:
            task: Failed task.
            error_message: Final error message.
        """
        import uuid

        dlq_id = str(uuid.uuid4())

        await self.conn.execute(
            """
            INSERT INTO dead_letter_queue
                (id, original_task_id, task_type, payload, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                dlq_id,
                task.id,
                task.task_type.value,
                json.dumps(task.payload),
                error_message,
            ),
        )

        await self.conn.execute(
            "DELETE FROM task_queue WHERE id = ?",
            (task.id,),
        )

        await self.conn.commit()

    async def get_dlq_count(self) -> int:
        """Get count of tasks in dead letter queue.

        Returns:
            Number of DLQ entries.
        """
        cursor = await self.conn.execute(
            "SELECT COUNT(*) as count FROM dead_letter_queue"
        )
        row = await cursor.fetchone()
        return row["count"] if row else 0

    async def get_dlq_entries(
        self,
        limit: int = 100,
        offset: int = 0,
        task_type: TaskType | None = None,
    ) -> list[DeadLetterEntry]:
        """Get entries from the dead letter queue.

        Args:
            limit: Maximum entries to return.
            offset: Number of entries to skip.
            task_type: Filter by task type (optional).

        Returns:
            List of DLQ entries.
        """
        if task_type:
            cursor = await self.conn.execute(
                """
                SELECT * FROM dead_letter_queue
                WHERE task_type = ?
                ORDER BY failed_at DESC
                LIMIT ? OFFSET ?
                """,
                (task_type.value, limit, offset),
            )
        else:
            cursor = await self.conn.execute(
                """
                SELECT * FROM dead_letter_queue
                ORDER BY failed_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )

        rows = await cursor.fetchall()
        return [self._row_to_dlq_entry(row) for row in rows]

    async def get_dlq_entry(self, dlq_id: str) -> DeadLetterEntry | None:
        """Get a specific DLQ entry by ID.

        Args:
            dlq_id: DLQ entry identifier.

        Returns:
            DLQ entry if found, None otherwise.
        """
        cursor = await self.conn.execute(
            "SELECT * FROM dead_letter_queue WHERE id = ?",
            (dlq_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_dlq_entry(row)

    async def retry_from_dlq(
        self,
        dlq_id: str,
        priority: TaskPriority = TaskPriority.HIGH,
    ) -> str | None:
        """Move a DLQ entry back to the task queue for retry.

        Args:
            dlq_id: DLQ entry identifier.
            priority: Priority for the retried task.

        Returns:
            New task ID if successful, None if entry not found.
        """
        import uuid

        # Get the DLQ entry
        entry = await self.get_dlq_entry(dlq_id)
        if entry is None:
            return None

        # Create new task
        new_task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        await self.conn.execute(
            """
            INSERT INTO task_queue
                (id, task_type, priority, payload, status, created_at, retry_count)
            VALUES (?, ?, ?, ?, 'pending', ?, 0)
            """,
            (
                new_task_id,
                entry.task_type.value,
                priority.value,
                json.dumps(entry.payload),
                now,
            ),
        )

        # Remove from DLQ
        await self.conn.execute(
            "DELETE FROM dead_letter_queue WHERE id = ?",
            (dlq_id,),
        )

        await self.conn.commit()
        return new_task_id

    async def delete_from_dlq(self, dlq_id: str) -> bool:
        """Permanently delete an entry from the DLQ.

        Args:
            dlq_id: DLQ entry identifier.

        Returns:
            True if deleted, False if not found.
        """
        cursor = await self.conn.execute(
            "DELETE FROM dead_letter_queue WHERE id = ? RETURNING id",
            (dlq_id,),
        )
        row = await cursor.fetchone()
        await self.conn.commit()
        return row is not None

    async def clear_dlq(self, task_type: TaskType | None = None) -> int:
        """Clear all entries from the DLQ.

        Args:
            task_type: Only clear entries of this type (optional).

        Returns:
            Number of entries deleted.
        """
        if task_type:
            cursor = await self.conn.execute(
                "DELETE FROM dead_letter_queue WHERE task_type = ?",
                (task_type.value,),
            )
        else:
            cursor = await self.conn.execute("DELETE FROM dead_letter_queue")

        await self.conn.commit()
        return cursor.rowcount

    async def get_dlq_stats(self) -> dict[str, int]:
        """Get DLQ statistics by task type.

        Returns:
            Dictionary of task_type -> count.
        """
        cursor = await self.conn.execute(
            """
            SELECT task_type, COUNT(*) as count
            FROM dead_letter_queue
            GROUP BY task_type
            """
        )
        rows = await cursor.fetchall()
        return {row["task_type"]: row["count"] for row in rows}

    def _row_to_dlq_entry(self, row: aiosqlite.Row) -> DeadLetterEntry:
        """Convert database row to DeadLetterEntry model."""
        return DeadLetterEntry(
            id=row["id"],
            original_task_id=row["original_task_id"],
            task_type=TaskType(row["task_type"]),
            payload=json.loads(row["payload"]),
            error_message=row["error_message"],
            failed_at=datetime.fromisoformat(row["failed_at"]),
        )

    # -------------------------------------------------------------------------
    # Decision Log Operations
    # -------------------------------------------------------------------------

    async def log_decision(
        self,
        task_id: str,
        entry_id: str | None,
        decision: str,
        confidence: float | None = None,
        reason: str | None = None,
        model_used: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
    ) -> None:
        """Log a curation decision for auditing.

        Args:
            task_id: Associated task ID.
            entry_id: Entry being evaluated.
            decision: Decision made.
            confidence: Confidence score.
            reason: Decision reason.
            model_used: LLM model used.
            tokens_input: Input tokens consumed.
            tokens_output: Output tokens generated.
        """
        await self.conn.execute(
            """
            INSERT INTO decision_log
                (task_id, entry_id, decision, confidence, reason,
                 model_used, tokens_input, tokens_output)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                entry_id,
                decision,
                confidence,
                reason,
                model_used,
                tokens_input,
                tokens_output,
            ),
        )
        await self.conn.commit()

    # -------------------------------------------------------------------------
    # Cost Tracking Operations
    # -------------------------------------------------------------------------

    async def record_cost(
        self,
        date: str,
        model: str,
        tokens_input: int,
        tokens_output: int,
        estimated_cost_usd: float,
    ) -> None:
        """Record or update cost for a model on a date.

        Args:
            date: Date string (YYYY-MM-DD).
            model: Model name.
            tokens_input: Input tokens used.
            tokens_output: Output tokens generated.
            estimated_cost_usd: Estimated cost in USD.
        """
        await self.conn.execute(
            """
            INSERT INTO cost_log
                (date, model, tokens_input, tokens_output, estimated_cost_usd, task_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(date, model) DO UPDATE SET
                tokens_input = tokens_input + excluded.tokens_input,
                tokens_output = tokens_output + excluded.tokens_output,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                task_count = task_count + 1
            """,
            (date, model, tokens_input, tokens_output, estimated_cost_usd),
        )
        await self.conn.commit()

    async def get_daily_cost(self, date: str) -> float:
        """Get total cost for a specific date.

        Args:
            date: Date string (YYYY-MM-DD).

        Returns:
            Total cost in USD.
        """
        cursor = await self.conn.execute(
            "SELECT SUM(estimated_cost_usd) as total FROM cost_log WHERE date = ?",
            (date,),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0.0
        return row["total"] or 0.0

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _row_to_task(self, row: aiosqlite.Row) -> QueuedTask:
        """Convert database row to QueuedTask model."""
        return QueuedTask(
            id=row["id"],
            task_type=TaskType(row["task_type"]),
            priority=TaskPriority(row["priority"]),
            payload=json.loads(row["payload"]),
            status=TaskStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            retry_count=row["retry_count"],
            error_message=row["error_message"],
        )


async def init_db(db_path: Path | None = None) -> Repository:
    """Initialize database and return repository instance.

    Args:
        db_path: Path to database file. Defaults to ./curator.db.

    Returns:
        Connected repository instance.
    """
    if db_path is None:
        db_path = Path("./curator.db")

    repo = Repository(db_path)
    await repo.connect()
    return repo
