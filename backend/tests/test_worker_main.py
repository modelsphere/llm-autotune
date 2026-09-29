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
    monkeypatch.setattr(worker_module, "wait_for_schema", lambda url: None)
    monkeypatch.setattr(worker_module, "ensure_screen_benchmark", lambda: None)
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: (_ for _ in ()).throw(StopTick()))

    with pytest.raises(StopTick):
        worker_module.main()

    assert len(acquires) == 1, "the lock must be acquired exactly once"
    assert len(releases) == 1, "the lock must be released on exit"


# -- startup ordering ----------------------------------------------------------


class _Stop(Exception):
    pass


def test_wait_for_schema_waits_until_the_migrate_job_has_run(monkeypatch, tmp_path):
    """The worker starts before the post-install migrate job. It must wait for
    the schema, not crash-loop (which also blocks `helm install --wait`)."""
    import sqlalchemy as sa

    import app.worker as worker_module

    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = sa.create_engine(url)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the "migrate job" runs while the worker waits
            with engine.begin() as connection:
                connection.execute(
                    sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))")
                )
        if len(sleeps) == 3:
            with engine.begin() as connection:
                connection.execute(sa.text("INSERT INTO alembic_version VALUES ('002_crd_group')"))
        if len(sleeps) > 5:
            raise _Stop

    monkeypatch.setattr(worker_module.time, "sleep", fake_sleep)
    worker_module.wait_for_schema(url, poll_seconds=0)
    assert len(sleeps) == 3, "returns once alembic_version holds a revision, not before"


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_screen_benchmark_is_retried_with_backoff_until_it_exists(monkeypatch):
    import app.worker as worker_module
    from app.evaluation.benchmarks import Ensured

    outcomes = [RuntimeError("connection refused"), RuntimeError("401"),
                Ensured("autotune-screen-v1", 3, True, True)]
    calls: list[float] = []
    clock = _Clock()

    def fake_ensure():
        calls.append(clock.now)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker_module, "ensure_screen_benchmark", fake_ensure)
    ensurer = worker_module.ScreenBenchmarkEnsurer(clock=clock)
    for t in (0, 10, 29, 30, 60, 89, 90, 200, 400):
        clock.now = t
        ensurer.poll()
    assert calls == [0, 30, 90], "backs off 30s, then 60s, and stops once it exists"
    assert ensurer.done


def test_a_refused_screen_benchmark_is_not_retried(monkeypatch):
    import app.worker as worker_module
    from app.evaluation.benchmarks import BenchmarkRefused

    calls: list[int] = []

    def refuse():
        calls.append(1)
        raise BenchmarkRefused("not ours")

    monkeypatch.setattr(worker_module, "ensure_screen_benchmark", refuse)
    clock = _Clock()
    ensurer = worker_module.ScreenBenchmarkEnsurer(clock=clock)
    for t in (0, 1000, 5000):
        clock.now = t
        ensurer.poll()
    assert calls == [1]
