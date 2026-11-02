"""Create-or-migrate: the one schema entrypoint a deployment calls.

`alembic upgrade head` alone cannot initialize a fresh database here:
001_initial materializes *current* model metadata (a squashed baseline), so
on an empty database it creates today's full schema — and every later
revision then re-adds a column that already exists. The chain only ever ran
against databases that predate it, which is exactly the kind of fact a
containerized deploy surfaces on its first `docker compose up`.

So the rule lives in code, once:

    no alembic_version table  ->  create_all from models, stamp head
    anything else             ->  alembic upgrade head

Existing deployments keep upgrading incrementally,
untouched. A database that has tables but no alembic_version is treated as
fresh-shaped: create_all(checkfirst) adds only what is missing and the stamp
records today — loudly, because if that schema was actually old, hand
reconciliation is owed and pretending otherwise would hide it.
"""

import logging
import sys
import time

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app import plugins
from app.core.config import get_settings
from app.db import models  # noqa: F401  — register every table on Base.metadata
from app.db.base import Base
from app.metrics_catalog import REPLAY

logger = logging.getLogger("bootstrap")
# Its own level, not the root's: running migrations applies alembic.ini's
# logging config, which drops the root to WARN, and what this job seeded
# afterwards is exactly what an operator reads its log for.
logger.setLevel(logging.INFO)


# Built-in objectives, so the campaign form has a sensible default on day one
# and the shapes an objective can take are visible by example. The sweep ones
# rank on what a throughput sweep reports (the default screen benchmark, or a
# sweep workload); the replay ones on what a replay of recorded traffic does.
BUILTIN_OBJECTIVES = [
    {
        "name": "Throughput per GPU",
        "description": "Default. Output tokens per minute per GPU — fair across tp/dp variants.",
        "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
        "direction": "maximize",
        "redlines": [],
    },
    {
        "name": "Throughput per GPU under a 5s TTFT SLO",
        "description": "Same, but a config whose p99 time to first token passes 5s does not count.",
        "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
        "direction": "maximize",
        "redlines": [{"metric": "perf_guidellm_sweep.ttft_p99_ms", "op": "<=", "value": 5000}],
    },
    {
        "name": "Absolute throughput (one service)",
        "description": "Most output tokens per second from one service, GPU count ignored.",
        "target_metric": "perf_guidellm_sweep.output_tps",
        "direction": "maximize",
        "redlines": [],
    },
    {
        "name": "Lowest time to first token",
        "description": "Latency first: the lowest p99 time to first token.",
        "target_metric": "perf_guidellm_sweep.ttft_p99_ms",
        "direction": "minimize",
        "redlines": [],
    },
    # For a campaign measured by replaying recorded traffic: the metric names
    # are the replay module's, which a sweep objective would never find.
    {
        "name": "Replay: throughput per GPU",
        "description": "Output tokens per minute per GPU on replayed real traffic.",
        "target_metric": f"{REPLAY}.output_tpm_card_norm",
        "direction": "maximize",
        "redlines": [],
    },
    {
        "name": "Replay: throughput per GPU under a 10s TTFT SLO",
        "description": "Same, but a config whose p99 time to first token passes 10s "
                       "does not count.",
        "target_metric": f"{REPLAY}.output_tpm_card_norm",
        "direction": "maximize",
        "redlines": [{"metric": f"{REPLAY}.ttft_p99_ms", "op": "<=", "value": 10000}],
    },
]


def main(argv: list[str] | None = None) -> None:
    """`--schema-only`: migrate and stop. The chart runs that before an
    upgrade replaces any pod, so the new code never meets the old schema (it
    answered 500 until the migration caught up). The full run, seeding
    included, follows once the new release's settings are in place."""
    args = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()
    engine = create_engine(settings.sync_database_url)
    config = Config("alembic.ini")
    _wait_for_database(engine)

    _platform_schema(engine, config)
    # A plugin's schema comes after the platform's (its tables may point at
    # the platform's) and before seeding; a plugin migration that fails stops
    # the job like the platform's own would.
    enabled = plugins.enabled()
    for plugin in enabled:
        plugins.upgrade(plugin, settings.sync_database_url)
    if "--schema-only" in args:
        return
    _seed(engine)
    for plugin in enabled:
        if plugin.on_bootstrap is None:
            continue
        try:
            plugin.on_bootstrap(engine)
        except Exception:
            logger.exception("plugin %s: bootstrap failed", plugin.name)


def _platform_schema(engine, config: Config) -> None:
    inspector = inspect(engine)
    if inspector.has_table("alembic_version"):
        logger.info("alembic_version present — upgrading to head")
        command.upgrade(config, "head")
        return

    tables = [
        t
        for t in inspector.get_table_names()
        if t in Base.metadata.tables  # not the alembic_version_* of a plugin, nor its tables
    ]
    if tables:
        logger.warning(
            "database has %d table(s) but no alembic_version — treating as "
            "current-shaped: create_all fills gaps, then stamping head. If this "
            "schema is actually OLD, reconcile it by hand before trusting the stamp.",
            len(tables),
        )
    else:
        logger.info("fresh database — creating current schema and stamping head")
    Base.metadata.create_all(engine)
    command.stamp(config, "head")


def _wait_for_database(engine, timeout_seconds: float = 600, poll_seconds: float = 3) -> None:
    """Wait for Postgres to accept connections.

    On a first install the database starts alongside this job, and failing
    the attempt only to be retried by the Job controller leaves an Error pod
    behind that looks like a broken install. Waiting says what is going on."""
    deadline = time.monotonic() + timeout_seconds
    waiting = False
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            if waiting:
                logger.info("database is up")
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            if not waiting:
                logger.info("waiting for the database (%s)", str(exc).splitlines()[0])
                waiting = True
            time.sleep(poll_seconds)


def _seed(engine) -> None:
    """What a fresh install needs to be usable, re-checked on every deploy.

    Each step is independent and only ever ADDITIVE: one that has nothing to
    do, or fails, never stops the others, and none of them overwrites a row
    someone may have edited.
    """
    _seed_admin(engine)
    _seed_objectives(engine)
    _seed_policies(engine)
    _register_local_cluster(engine)
    _ensure_screen_benchmark()


def _seed_admin(engine) -> None:
    """The first admin, so a fresh install has someone who can log in.

    Only when the users table is EMPTY: anything this overwrote would silently
    undo an admin's own edit — including a changed password.

    Credentials come from AUTOTUNE_ADMIN_USERNAME / AUTOTUNE_ADMIN_PASSWORD.
    With no password set nothing is created and the log says so, because a
    platform that ships a known default password is worse than one that makes
    you type a value into your values file.
    """
    from sqlalchemy.orm import Session

    from app.core.auth import hash_password
    from app.db.models import User, UserRole

    settings = get_settings()
    try:
        with Session(engine) as session:
            if session.query(User.id).first() is not None:
                return  # somebody already has an account; never touch it
            if not settings.admin_password:
                logger.info(
                    "no AUTOTUNE_ADMIN_PASSWORD set — no admin seeded. Set one and "
                    "redeploy, or create the first user another way."
                )
                return
            session.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    role=UserRole.ADMIN.value,
                )
            )
            session.commit()
        logger.info("seeded the first admin user %r", settings.admin_username)
    except Exception:  # noqa: BLE001 — a seed must never block a deploy
        logger.exception("admin seeding failed — no user was created")


def _seed_objectives(engine) -> None:
    """The built-in objectives, each added only if no objective has its name.

    Built-ins cannot be edited or deleted from the API, so a missing one was
    never there; a user objective that happens to share a name is theirs and
    is left alone."""
    from sqlalchemy.orm import Session

    from app.db.models import Objective

    try:
        with Session(engine) as session:
            existing = {name for (name,) in session.query(Objective.name)}
            added = [spec["name"] for spec in BUILTIN_OBJECTIVES if spec["name"] not in existing]
            for spec in BUILTIN_OBJECTIVES:
                if spec["name"] in added:
                    session.add(Objective(owner_id=None, is_builtin=True, **spec))
            session.commit()
        if added:
            logger.info("added built-in objectives: %s", ", ".join(added))
    except Exception:  # noqa: BLE001 — never block a deploy
        logger.exception("could not seed the built-in objectives")


def _seed_policies(engine) -> None:
    """The search policies an install comes with (AUTOTUNE_DEFAULT_POLICIES, a
    JSON list the chart's `defaultPolicies` writes), each added only while no
    policy has its name: one an admin re-pointed at another image, or deleted
    and re-added, is theirs. Owned by the first admin."""
    import json

    from sqlalchemy.orm import Session

    from app.db.models import Policy, User, UserRole

    raw = get_settings().default_policies.strip()
    if not raw:
        return
    try:
        wanted = json.loads(raw)
        if not isinstance(wanted, list):
            raise ValueError("not a list")
    except ValueError as exc:
        logger.error("AUTOTUNE_DEFAULT_POLICIES is not a JSON list (%s); no policy seeded", exc)
        return
    try:
        with Session(engine) as session:
            admin = (session.query(User).filter(User.role == UserRole.ADMIN.value)
                     .order_by(User.id).first())
            if admin is None:
                logger.info("no admin yet; default policies wait for one")
                return
            for entry in wanted:
                name = str(entry.get("name", "")).strip()
                image = str(entry.get("image", "")).strip()
                if not name or not image:
                    logger.error("default policy %r needs a name and an image; skipped", entry)
                    continue
                if session.query(Policy.id).filter(Policy.name == name).first() is not None:
                    continue
                session.add(Policy(
                    owner_id=admin.id, name=name, image=image,
                    version=str(entry.get("version") or image.rsplit(":", 1)[-1]),
                    description=str(entry.get("description", "")),
                    repo_url=str(entry.get("repoUrl", entry.get("repo_url", ""))),
                    gpus_in_container=bool(entry.get("gpusInContainer", False)),
                    needs_model=bool(entry.get("needsModel", False)),
                    env={},
                ))
                logger.info("registered the default policy %r (%s)", name, image)
            session.commit()
    except Exception:  # noqa: BLE001 — a seed must never block a deploy
        logger.exception("default policy seeding failed")


def _ensure_screen_benchmark() -> None:
    """Create and lock AutoTune's screening benchmark on LLMBench, if it can be
    reached. Never fatal: LLMBench is a separate install that may come up after
    this one, and the worker keeps trying until it succeeds."""
    from app.evaluation.benchmarks import ensure_screen_benchmark

    try:
        ensured = ensure_screen_benchmark()
    except Exception as exc:  # unreachable, no service key yet, refused
        logger.info("screening benchmark not ensured yet (%s); the worker will retry", exc)
        return
    if ensured is not None:
        logger.info("screening benchmark %s %s and locked", ensured.slug,
                    "created" if ensured.created else "present")


def _register_local_cluster(engine) -> None:
    """Offer the cluster this pod runs in, once.

    A fresh install otherwise lands on an empty Resources page, and the first
    thing anyone has to do is describe the cluster the platform is already
    running inside. Only when there is NO cluster and NO machine: the moment
    someone has described a fleet of their own, this stays out of the way for
    good.

    The cluster row takes the AUTOTUNE_K8S_* settings the chart writes: they
    describe this cluster (in-cluster access, Service-name endpoints, the
    site's chart repository and image mirror), so they are pinned to its row
    and not to the clusters added later, which carry their own. With
    auto_register_machine it also gets one machine over all of its nodes, the
    scheduler placing each pod; card count and type are left at zero for the
    capacity probe to fill from the nodes themselves.
    """
    from sqlalchemy.orm import Session

    from app.db.models import Cluster, Machine, MachineState

    settings = get_settings()
    if not (settings.auto_register_cluster and settings.k8s_in_cluster):
        return
    try:
        with Session(engine) as session:
            if (session.query(Machine.id).first() is not None
                    or session.query(Cluster.id).first() is not None):
                return
            cluster = Cluster(
                name=settings.local_cluster_name,
                notes="The cluster AutoTune runs in, registered automatically on an empty install.",
                api_mode="client",
                in_cluster=True,
                namespace=settings.k8s_namespace,
                workload_kind=settings.k8s_workload_kind,
                chart_repo=settings.k8s_chart_repo,
                chart_credentials_secret=settings.k8s_chart_credentials_secret,
                endpoint=settings.k8s_endpoint or "dns",
                image_mirror=settings.k8s_image_mirror,
                site_values=settings.k8s_site_values,
                gpu_resource=settings.k8s_gpu_resource,
                gpu_product_label=settings.k8s_gpu_product_label,
                runtime_class=settings.k8s_runtime_class,
                tolerate_gpu_taint=settings.k8s_tolerate_gpu_taint,
                tolerations=settings.k8s_tolerations,
                image_pull_secrets=settings.k8s_image_pull_secrets,
                model_pvc=settings.k8s_model_pvc,
                model_pvc_root=settings.k8s_model_pvc_root,
                shm_size_mb=settings.k8s_shm_size_mb,
                node_selector=settings.k8s_node_selector,
            )
            session.add(cluster)
            session.flush()
            if settings.auto_register_machine:
                session.add(
                    Machine(
                        name="local-cluster",
                        host="",  # k8s placement is by selector, never by address
                        driver="k8s",
                        cluster_id=cluster.id,
                        gpu_count=0,  # filled by the capacity probe
                        state=MachineState.AWAY.value,
                        notes=(
                            "Every node of the cluster this release runs in, registered "
                            "automatically on an empty install. Probe it from the Resources "
                            "page to fill in its GPUs, then mark it available."
                        ),
                    )
                )
            session.commit()
        logger.info("registered the local cluster as %r%s", settings.local_cluster_name,
                    " with machine 'local-cluster'" if settings.auto_register_machine else "")
    except Exception:  # noqa: BLE001 — never block a deploy
        logger.exception("could not register the local cluster")


if __name__ == "__main__":
    main()
