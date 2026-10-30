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
    monkeypatch.setattr(bootstrap, "_seed_policies", lambda e: ran.append("policies"))
    monkeypatch.setattr(bootstrap, "_register_local_cluster", lambda e: ran.append("cluster"))
    monkeypatch.setattr(bootstrap, "_ensure_screen_benchmark", lambda: ran.append("benchmark"))
    bootstrap._seed(engine)
    assert ran == ["policies", "cluster", "benchmark"]
    with Session(engine) as session:
        assert session.query(User).count() == 1
        assert session.query(Objective).count() == len(bootstrap.BUILTIN_OBJECTIVES)


def test_schema_only_migrates_and_seeds_nothing(monkeypatch):
    # The chart's pre-upgrade Job: the schema before any new pod starts, and
    # nothing that needs the new release's settings (the seed follows later).
    steps: list[str] = []
    monkeypatch.setattr(bootstrap, "create_engine", lambda url: object())
    monkeypatch.setattr(bootstrap, "_wait_for_database", lambda e: steps.append("wait"))
    monkeypatch.setattr(bootstrap, "_platform_schema", lambda e, c: steps.append("schema"))
    monkeypatch.setattr(bootstrap, "_seed", lambda e: steps.append("seed"))
    bootstrap.main(["--schema-only"])
    assert steps == ["wait", "schema"]
    steps.clear()
    bootstrap.main([])
    assert steps == ["wait", "schema", "seed"]


def _settings(monkeypatch, **update):
    from app.core.config import get_settings

    settings = get_settings().model_copy(update=update)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    return settings


IN_CLUSTER = dict(
    k8s_in_cluster=True, auto_register_cluster=True, k8s_namespace="llm-autotune-runs",
    k8s_workload_kind="llmservice", k8s_endpoint="dns",
    k8s_chart_repo="oci://registry.example/charts", k8s_image_mirror="registry.example/team",
)


def test_an_empty_in_cluster_install_registers_its_cluster_and_one_machine(monkeypatch):
    from app.control.launch.clusters import K8sClusterSettings
    from app.core.config import get_settings
    from app.db.models import Cluster, Machine

    settings = _settings(monkeypatch, **IN_CLUSTER)
    engine = _engine()
    bootstrap._register_local_cluster(engine)
    bootstrap._register_local_cluster(engine)  # every deploy: still one of each
    with Session(engine) as session:
        cluster = session.query(Cluster).one()
        machine = session.query(Machine).one()
        assert (cluster.name, cluster.in_cluster, cluster.namespace, cluster.endpoint) == (
            "local", True, "llm-autotune-runs", "dns")
        assert (cluster.chart_repo, cluster.image_mirror) == (
            "oci://registry.example/charts", "registry.example/team")
        assert (machine.name, machine.driver, machine.cluster_id) == (
            "local-cluster", "k8s", cluster.id)
        # The row describes this cluster on its own: a cluster added later starts
        # from the install's defaults, and its own columns win over them.
        resolved = K8sClusterSettings.from_row(cluster, get_settings())
        assert resolved.k8s_in_cluster and resolved.k8s_image_mirror == settings.k8s_image_mirror


def test_the_cluster_alone_when_its_machines_are_registered_by_hand(monkeypatch):
    from app.db.models import Cluster, Machine

    _settings(monkeypatch, **IN_CLUSTER, auto_register_machine=False, local_cluster_name="gpu-a")
    engine = _engine()
    bootstrap._register_local_cluster(engine)
    with Session(engine) as session:
        assert session.query(Cluster).one().name == "gpu-a"
        assert session.query(Machine).count() == 0
    # Its machines get registered later; deleting them never brings one back.
    bootstrap._register_local_cluster(engine)
    with Session(engine) as session:
        assert session.query(Machine).count() == 0


def test_a_described_fleet_is_left_alone(monkeypatch):
    from app.db.models import Cluster

    _settings(monkeypatch, **IN_CLUSTER)
    engine = _engine()
    with Session(engine) as session:
        session.add(Cluster(name="theirs"))
        session.commit()
    bootstrap._register_local_cluster(engine)
    with Session(engine) as session:
        assert [c.name for c in session.query(Cluster)] == ["theirs"]


def test_default_policies_are_added_once_and_never_overwritten(monkeypatch):
    import json

    from app.db.models import Policy

    image = "docker.io/4pdosc/llm-autotune-policy-random-search:0.1.1"
    _settings(monkeypatch, default_policies=json.dumps([
        {"name": "random-search", "image": image, "description": "Samples at random."},
        {"name": "broken"},
    ]))
    engine = _engine()
    bootstrap._seed_policies(engine)  # no admin yet: waits
    with Session(engine) as session:
        assert session.query(Policy).count() == 0
        session.add(User(username="ana", password_hash="x", role="admin"))
        session.commit()
    bootstrap._seed_policies(engine)
    with Session(engine) as session:
        policy = session.query(Policy).one()
        assert (policy.name, policy.image, policy.version) == ("random-search", image, "0.1.1")
        assert not policy.gpus_in_container and not policy.needs_model
        policy.image = "registry.example/random-search:mine"
        session.commit()
    bootstrap._seed_policies(engine)
    with Session(engine) as session:
        assert session.query(Policy).one().image == "registry.example/random-search:mine"


def test_default_policies_that_are_not_a_list_seed_nothing(monkeypatch):
    from app.db.models import Policy

    _settings(monkeypatch, default_policies='{"name": "x"}')
    engine = _engine()
    with Session(engine) as session:
        session.add(User(username="ana", password_hash="x", role="admin"))
        session.commit()
    bootstrap._seed_policies(engine)
    with Session(engine) as session:
        assert session.query(Policy).count() == 0
