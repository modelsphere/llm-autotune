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

Existing deployments (the devbox at revision N) keep upgrading incrementally,
untouched. A database that has tables but no alembic_version is treated as
fresh-shaped: create_all(checkfirst) adds only what is missing and the stamp
records today — loudly, because if that schema was actually old, hand
reconciliation is owed and pretending otherwise would hide it.
"""

import logging

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings
from app.db import models  # noqa: F401  — register every table on Base.metadata
from app.db.base import Base

logger = logging.getLogger("bootstrap")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()
    engine = create_engine(settings.sync_database_url)
    config = Config("alembic.ini")

    inspector = inspect(engine)
    if inspector.has_table("alembic_version"):
        logger.info("alembic_version present — upgrading to head")
        command.upgrade(config, "head")
        _seed(engine)
        return

    tables = [t for t in inspector.get_table_names() if t != "alembic_version"]
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
    _seed(engine)


def _seed(engine) -> None:
    """The first admin, so a fresh install has someone who can log in.

    Only ever ADDITIVE, and only when the users table is EMPTY: seeding runs
    on every deploy, so anything it overwrote would silently undo an admin's
    own edit — including a changed password. A deployment that already has
    users is left alone entirely.

    Credentials come from AUTOTUNE_ADMIN_USERNAME / AUTOTUNE_ADMIN_PASSWORD.
    With no password set nothing is created and the log says so, because a
    platform that ships a known default password is worse than one that makes
    you type a value into your values file.
    """
    from sqlalchemy.orm import Session

    from app.core.auth import hash_password
    from app.db.models import User, UserRole

    settings = get_settings()
    if not settings.admin_password:
        logger.info(
            "no AUTOTUNE_ADMIN_PASSWORD set — no admin seeded. Set one and redeploy, "
            "or create the first user another way."
        )
        return
    try:
        with Session(engine) as session:
            if session.query(User.id).first() is not None:
                return  # somebody already has an account; never touch it
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
    _register_local_cluster(engine)
    _ensure_screen_benchmark()


def _ensure_screen_benchmark() -> None:
    """Create and lock AutoTune's screening benchmark on LLMBench, if it can be
    reached. Never fatal: LLMBench is a separate install that may come up after
    this one, and the same thing can be done later from the campaign form or
    POST /api/benchmarks/ensure."""
    settings = get_settings()
    if not settings.llmbench_ensure_benchmarks or not settings.llmbench_base_url:
        return
    from app.evaluation.benchmarks import ensure_benchmark
    from app.evaluation.llmbench import LLMBenchClient

    try:
        ensured = ensure_benchmark(LLMBenchClient(max_attempts=1))
    except Exception as exc:  # unreachable, no service key yet, refused
        logger.info("screening benchmark not ensured now (%s); ensure it later from the UI", exc)
        return
    logger.info("screening benchmark %s %s and locked", ensured.slug,
                "created" if ensured.created else "present")


def _register_local_cluster(engine) -> None:
    """Offer the cluster this pod runs in as a machine pool, once.

    A fresh install otherwise lands on an empty Resources page, and the first
    thing anyone has to do is describe the cluster the platform is already
    running inside. Only when the fleet is EMPTY: the moment someone has added
    a machine of their own, this stays out of the way for good.

    The row points at the default cluster (no `cluster_id`), which reads the
    AUTOTUNE_K8S_* settings the chart writes — so there is one description of
    the cluster, not two that can disagree. Card count and card type are left
    at zero for the capacity probe to fill from the nodes themselves.
    """
    from sqlalchemy.orm import Session

    from app.db.models import Machine, MachineState

    settings = get_settings()
    if not (settings.auto_register_cluster and settings.k8s_in_cluster):
        return
    try:
        with Session(engine) as session:
            if session.query(Machine.id).first() is not None:
                return
            session.add(
                Machine(
                    name="local-cluster",
                    host="",  # k8s placement is by selector, never by address
                    driver="k8s",
                    gpu_count=0,  # filled by the capacity probe
                    state=MachineState.AWAY.value,
                    notes=(
                        "The cluster this release runs in, registered automatically on "
                        "an empty install. Probe it from the Resources page to fill in "
                        "its GPUs, then mark it available."
                    ),
                )
            )
            session.commit()
        logger.info("registered the local cluster as machine 'local-cluster'")
    except Exception:  # noqa: BLE001 — never block a deploy
        logger.exception("could not register the local cluster")


if __name__ == "__main__":
    main()
