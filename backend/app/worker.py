"""Orchestrator worker entrypoint: `python -m app.worker`.

A supervisor over the Postgres run state machine — reconciles once at startup
(re-attach, never re-run), then ticks forever.
"""

import logging
import sys
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app.control.orchestrator.singleton import WorkerLock, WorkerLockError
from app.control.orchestrator.supervisor import Supervisor
from app.core.config import get_settings
from app.db.base import sync_session_factory
from app.evaluation.benchmarks import BenchmarkRefused, ensure_screen_benchmark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


def wait_for_schema(url: str, poll_seconds: float = 5.0) -> None:
    """Block until the database is up and the migrate job has created the schema.

    On an install the worker starts alongside the database and before the
    migrate job (a post-install hook) has run. Exiting would crash-loop the
    pod until then, and under `helm install --wait` the install would never
    finish, since the hook only runs once this pod is up. Waiting says what is
    going on instead."""
    engine = create_engine(url)
    reason = ""
    try:
        while True:
            try:
                with engine.connect() as connection:
                    if inspect(connection).has_table("alembic_version") and connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).first():
                        if reason:
                            logger.info("schema is ready")
                        return
                now = "the schema (the migrate job creates it)"
            except OperationalError as exc:
                now = f"the database ({str(exc).splitlines()[0]})"
            if now != reason:
                logger.info("waiting for %s", now)
                reason = now
            time.sleep(poll_seconds)
    finally:
        engine.dispose()


class ScreenBenchmarkEnsurer:
    """Creates AutoTune's screen benchmark on LLMBench, retrying until it exists.

    The migrate job tries once, but LLMBench is a separate install that may
    come up later, or seed its service account later. Without a retry every
    campaign would fail at its first submission until someone pressed
    "ensure" in the UI. Backs off to ten minutes, and logs the first failure
    and the outcome rather than every attempt."""

    MAX_BACKOFF_SECONDS = 600

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.done = False
        self.next_at = 0.0
        self.backoff = 30.0
        self.warned = False

    def poll(self) -> None:
        if self.done or self.clock() < self.next_at:
            return
        try:
            ensured = ensure_screen_benchmark()
        except BenchmarkRefused as exc:
            logger.error("%s", exc)  # someone else's benchmark: retrying cannot fix it
            self.done = True
            return
        except Exception as exc:  # noqa: BLE001 — unreachable, no service key yet
            if not self.warned:
                logger.warning(
                    "screen benchmark not ensured on LLMBench yet (%s); retrying in the "
                    "background", exc,
                )
                self.warned = True
            self.next_at = self.clock() + self.backoff
            self.backoff = min(self.backoff * 2, self.MAX_BACKOFF_SECONDS)
            return
        self.done = True
        if ensured is not None and (self.warned or ensured.created):
            logger.info("screen benchmark %s %s and locked on LLMBench", ensured.slug,
                        "created" if ensured.created else "present")


def main() -> None:
    settings = get_settings()
    wait_for_schema(settings.sync_database_url)
    lock = WorkerLock()
    try:
        lock.acquire()  # exactly once — a second acquire would block on itself
    except WorkerLockError as exc:
        logger.error("refusing to start: %s", exc)
        sys.exit(1)

    try:
        supervisor = Supervisor(session_factory=sync_session_factory)
        ensurer = ScreenBenchmarkEnsurer()
        logger.info("worker starting; reconciling in-flight runs")
        supervisor.reconcile()
        logger.info("entering tick loop (every %ss)", settings.worker_tick_seconds)
        while True:
            ensurer.poll()
            try:
                supervisor.tick()
            except Exception:
                logger.exception("tick failed; continuing")
            time.sleep(settings.worker_tick_seconds)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
