"""Single-worker guarantee.

Two supervisors ticking the same database is catastrophic: both schedule the
same candidate, both launch containers on the same machine, and each tears
down the other's work (observed live: an orphaned worker plus a
fresh one raced and killed a run with "container disappeared").

A Postgres session-level advisory lock enforces one worker per database:
- held on a dedicated connection for the process lifetime
- released automatically when that connection drops (crash, kill -9, netsplit)
  — no TTL, no heartbeat, no reaper needed
"""

import logging
import time

from sqlalchemy import create_engine, text

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Arbitrary but fixed: identifies "the autotune orchestrator" lock.
WORKER_LOCK_KEY = 0x4155_544F  # "AUTO"


class WorkerLockError(RuntimeError):
    pass


class WorkerLock:
    """Context manager holding the single-worker advisory lock."""

    def __init__(self, key: int = WORKER_LOCK_KEY):
        self.key = key
        self._engine = None
        self._connection = None

    def acquire(self, wait_seconds: float = 30.0, poll_seconds: float = 2.0) -> None:
        """Take the lock, briefly waiting out a predecessor.

        A worker that was SIGKILLed leaves its Postgres backend (and therefore
        its lock) alive until the server notices the dead socket — seconds,
        typically. Retrying makes restart-after-kill reliable while still
        refusing to run beside a genuinely live worker.
        """
        settings = get_settings()
        # Dedicated engine/connection: pooled sessions get recycled, which
        # would drop a session-scoped lock.
        self._engine = create_engine(settings.sync_database_url, poolclass=None)
        self._connection = self._engine.connect()

        deadline = time.monotonic() + wait_seconds
        while True:
            acquired = self._connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}
            ).scalar()
            if acquired:
                logger.info("acquired single-worker advisory lock (key=%s)", hex(self.key))
                return
            if time.monotonic() >= deadline:
                self.release()
                raise WorkerLockError(
                    "another orchestrator worker is already running against this database "
                    f"(advisory lock still held after {wait_seconds:.0f}s). Stop it first: "
                    "docker compose down (or stop the worker however you started it)"
                )
            logger.warning("worker lock held (predecessor still shutting down?); retrying...")
            time.sleep(poll_seconds)

    def release(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
