"""What a fresh install seeds, and that every step runs on every deploy.

Regression: the seed returned as soon as a user existed, so on every upgrade
after the first install the local cluster was never registered and the screen
benchmark never re-ensured; and the built-in objectives were lost when the
migrations were squashed, which left the campaign form with nothing to pick.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import bootstrap
from app.db.base import Base
from app.db.models import Objective, User
from app.metrics_catalog import DEFAULT_TARGET_METRIC


def _engine():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_builtin_objectives_are_seeded_once_and_include_the_default_metric():
    engine = _engine()
    bootstrap._seed_objectives(engine)
    bootstrap._seed_objectives(engine)
    with Session(engine) as session:
        rows = session.query(Objective).all()
    assert len(rows) == len(bootstrap.BUILTIN_OBJECTIVES)
    assert all(r.is_builtin and r.owner_id is None for r in rows)
    # The campaign form preselects the built-in on the platform default metric.
    assert any(r.target_metric == DEFAULT_TARGET_METRIC for r in rows)


def test_each_builtin_objective_fits_one_kind_of_benchmark():
    """A sweep objective ranks on the screen benchmark's module, a replay one
    on the replay module's; none mixes the two, which no benchmark reports."""
    from app.evaluation.benchmark_spec import replay_module
    from app.evaluation.benchmarks import DEFAULT_SCREEN_TEMPLATE, load_template

    sweep = {m["module_name"] for m in load_template(DEFAULT_SCREEN_TEMPLATE)["modules"]}
    for spec in bootstrap.BUILTIN_OBJECTIVES:
        metrics = [spec["target_metric"], *(r["metric"] for r in spec["redlines"])]
        used = {m.split(".")[0] for m in metrics}
        assert used <= sweep or used == {replay_module()}, spec["name"]


def test_a_user_objective_with_a_builtin_name_is_left_alone():
    engine = _engine()
    with Session(engine) as session:
        session.add(User(username="ana", password_hash="x", role="admin"))
        session.flush()
        session.add(Objective(owner_id=1, name="Throughput per GPU", target_metric="x.y",
                              direction="minimize", redlines=[]))
        session.commit()
    bootstrap._seed_objectives(engine)
    with Session(engine) as session:
        mine = session.query(Objective).filter_by(name="Throughput per GPU").one()
        assert (mine.owner_id, mine.target_metric, mine.is_builtin) == (1, "x.y", False)
        assert session.query(Objective).count() == len(bootstrap.BUILTIN_OBJECTIVES)


def test_every_seed_step_runs_when_users_already_exist(monkeypatch):
    engine = _engine()
    with Session(engine) as session:
        session.add(User(username="ana", password_hash="x", role="admin"))
        session.commit()
    ran: list[str] = []
    monkeypatch.setattr(bootstrap, "_register_local_cluster", lambda e: ran.append("cluster"))
    monkeypatch.setattr(bootstrap, "_ensure_screen_benchmark", lambda: ran.append("benchmark"))
    bootstrap._seed(engine)
    assert ran == ["cluster", "benchmark"]
    with Session(engine) as session:
        assert session.query(User).count() == 1
        assert session.query(Objective).count() == len(bootstrap.BUILTIN_OBJECTIVES)
