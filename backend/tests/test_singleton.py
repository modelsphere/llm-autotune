"""The single-worker advisory lock. Needs a real Postgres (advisory locks are
a Postgres feature); skipped when AUTOTUNE_SYNC_DATABASE_URL isn't reachable."""

import pytest
from sqlalchemy import create_engine, text

from app.control.orchestrator.singleton import WorkerLock, WorkerLockError
from app.core.config import get_settings


def _postgres_available() -> bool:
    try:
        engine = create_engine(get_settings().sync_database_url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(), reason="no Postgres reachable for advisory-lock test"
)


def test_second_worker_is_refused():
    first = WorkerLock(key=0x7E57_0001)
    first.acquire()
    try:
        second = WorkerLock(key=0x7E57_0001)
        with pytest.raises(WorkerLockError, match="already running"):
            second.acquire(wait_seconds=0, poll_seconds=0)
    finally:
        first.release()


def test_acquire_waits_for_a_predecessor_then_succeeds():
    """A lock released during the wait window is picked up, not rejected."""
    import threading

    lock_key = 0x7E57_0003
    predecessor = WorkerLock(key=lock_key)
    predecessor.acquire()
    threading.Timer(1.0, predecessor.release).start()

    successor = WorkerLock(key=lock_key)
    successor.acquire(wait_seconds=15, poll_seconds=0.5)  # must not raise
    successor.release()


def test_lock_is_reusable_after_release():
    lock_key = 0x7E57_0002
    with WorkerLock(key=lock_key):
        pass
    # released → a fresh worker can take it
    with WorkerLock(key=lock_key):
        pass
