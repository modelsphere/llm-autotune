# Upgrading

## How the schema is applied

`helm install` and `helm upgrade` run `python -m app.db.bootstrap` as a Job once
the release is applied; the new API and worker start alongside it, and the
worker waits until the schema is there. The bootstrap is create-or-migrate:

- **No `alembic_version` table** — a fresh database. The current schema is
  created from the models and stamped at head.
- **Anything else** — `alembic upgrade head`.

A database with tables but no `alembic_version` is treated as current-shaped:
missing tables are filled in and head is stamped, loudly. If that schema was
actually old, reconcile it by hand before trusting the stamp.

Then, on every install and upgrade, it adds what is missing and never changes
what exists: the first admin (only while there are no users), the built-in
objectives, the `local-cluster` machine (only while there are no machines), and
the screen benchmark on LLMBench.

## Migrations

The chain starts at `001_initial`, and every schema change is an incremental
revision on top of it, so `helm upgrade` is all an upgrade needs. A plugin's
tables migrate on their own chain, in their own version table
([plugins](plugins.md)).

## Versions

The chart deploys the images tagged with its `appVersion`, so upgrading the chart
upgrades the platform. Set `image.tag` to hold the images at one version while
the chart moves, or to run a release candidate (`0.1.0-rc1`).
