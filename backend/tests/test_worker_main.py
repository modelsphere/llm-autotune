"""worker.main() lock lifecycle — no database needed."""

import pytest


def test_worker_main_acquires_the_lock_only_once(monkeypatch):
    """Regression: worker.main() acquired, then re-entered the context manager,
    deadlocking against its own lock (the second acquire uses a new session)."""
    import app.worker as worker_module

    acquires: list[int] = []
    releases: list[int] = []

    class SpyLock:
        def acquire(self, *args, **kwargs):
            acquires.append(1)

        def release(self):
            releases.append(1)

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

    class StopTick(Exception):
        pass

    class SpySupervisor:
        def __init__(self, *args, **kwargs):
            pass

        def reconcile(self):
            pass

        def tick(self):
            raise StopTick

    monkeypatch.setattr(worker_module, "WorkerLock", SpyLock)
    monkeypatch.setattr(worker_module, "Supervisor", SpySupervisor)
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: (_ for _ in ()).throw(StopTick()))

    with pytest.raises(StopTick):
        worker_module.main()

    assert len(acquires) == 1, "the lock must be acquired exactly once"
    assert len(releases) == 1, "the lock must be released on exit"
