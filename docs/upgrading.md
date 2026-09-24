# Upgrading

## How the schema is applied

`helm install` and `helm upgrade` run `python -m app.db.bootstrap` as a Job once
the release is applied; the new API and worker start alongside it, and the
worker restarts until the schema is there. The bootstrap is create-or-migrate:

- **No `alembic_version` table** — a fresh database. The current schema is
  created from the models and stamped at head.
- **Anything else** — `alembic upgrade head`.

A database with tables but no `alembic_version` is treated as current-shaped:
missing tables are filled in and head is stamped, loudly. If that schema was
actually old, reconcile it by hand before trusting the stamp.

## Migration compatibility

The public migration chain starts at `001_initial`, which materializes the
current models. It is **not** a continuation of any chain that predates the first
public release — a database from before that cannot be upgraded onto this one by
replaying revisions. Start a new database, or migrate your data across
deliberately.

From the first public release onward, every schema change is an ordinary
incremental revision on top of `001_initial`, and `helm upgrade` is all that is
needed.

## Versions

The chart deploys the images tagged with its `appVersion`, so upgrading the chart
upgrades the platform. Set `image.tag` to hold the images at one version while
the chart moves, or to run a release candidate (`0.1.0-rc1`).
