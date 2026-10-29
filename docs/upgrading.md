# Upgrading

## How the schema is applied

`helm install` runs `python -m app.db.bootstrap` as a Job once the release is
applied; the API and worker start alongside it, and the worker waits until the
schema is there. `helm upgrade` migrates first: a `pre-upgrade` Job runs
`python -m app.db.bootstrap --schema-only` before any pod is replaced, so the
new API never answers from an old schema. The pods still running meanwhile are
the old release's, against the new schema (see
[Migrations](#migrations)). The same bootstrap Job as on install follows the
upgrade and seeds. The bootstrap is create-or-migrate:

- **No `alembic_version` table** — a fresh database. The current schema is
  created from the models and stamped at head.
- **Anything else** — `alembic upgrade head`.

A database with tables but no `alembic_version` is treated as current-shaped:
missing tables are filled in and head is stamped, loudly. If that schema was
actually old, reconcile it by hand before trusting the stamp.

Then, on every install and upgrade, it adds what is missing and never changes
what exists: the first admin (only while there are no users), the built-in
objectives, the `defaultPolicies` (each only while no policy has its name),
the platform's own cluster and its `local-cluster` machine (only while there
are no clusters and no machines), and the screen benchmark on LLMBench.

## Migrations

The chain starts at `001_initial`, and every schema change is an incremental
revision on top of it, so `helm upgrade` is all an upgrade needs. A migration
only adds (a table, a nullable or defaulted column): during an upgrade the old
release's pods run against the new schema until they are replaced, and a
rename or a drop would break them. Remove something a release after the code
stopped using it.

An upgrade drops no requests and keeps the data:

- The API and frontend pods are replaced one at a time, and a pod being
  replaced keeps serving for a few seconds while the Service stops sending it
  requests. A policy running through the upgrade sees no error (its SDK also
  retries a 5xx for about a minute).
- The bundled Postgres restarts only when its own settings change (its image,
  its resources), not on every chart version. With
  `postgresql.storage.enabled=false` a restart empties the database, so leave
  storage on anywhere the data matters.

Two things an upgrade cannot change: the Postgres volume's size and storage
class (a StatefulSet's volume claim is fixed once created), and the
`runLogs` claim's storage class. `helm uninstall` leaves the Postgres volume
behind: a new install in the same namespace finds the old database, which keeps
its old password. Reuse the old `postgresql.password`, or delete the claim
(`data-<release>-llm-autotune-postgresql-0`) to start clean.
`deploy/quickstart.sh down` deletes the namespaces and the volumes with them. A plugin's
tables migrate on their own chain, in their own version table
([plugins](plugins.md)).

## Versions

The chart deploys the images tagged with its `appVersion`, so upgrading the chart
upgrades the platform. Set `image.tag` to hold the images at one version while
the chart moves, or to run a release candidate (`0.1.0-rc1`).
