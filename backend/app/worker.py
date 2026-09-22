"""Orchestrator worker entrypoint: `python -m app.worker`.

A supervisor over the Postgres run state machine — reconciles once at startup
(re-attach, never re-run), then ticks forever.
"""

import logging
import sys
import time

from app.control.orchestrator.singleton import WorkerLock, WorkerLockError
from app.control.orchestrator.supervisor import Supervisor
from app.core.config import get_settings
from app.db.base import sync_session_factory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


def main() -> None:
    settings = get_settings()
    lock = WorkerLock()
    try:
        lock.acquire()  # exactly once — a second acquire would block on itself
    except WorkerLockError as exc:
        logger.error("refusing to start: %s", exc)
        sys.exit(1)

    try:
        supervisor = Supervisor(session_factory=sync_session_factory)
        logger.info("worker starting; reconciling in-flight runs")
        supervisor.reconcile()
        logger.info("entering tick loop (every %ss)", settings.worker_tick_seconds)
        while True:
            try:
                supervisor.tick()
            except Exception:
                logger.exception("tick failed; continuing")
            time.sleep(settings.worker_tick_seconds)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
