# Contributing

## What you need

| | |
|---|---|
| Backend | Python 3.12 and [uv](https://docs.astral.sh/uv/) |
| Frontend | Node 22 |
| Operator | Go 1.26 (only if you touch `operator/`) |
| Both | Docker, for the datastores and for building images |

## Running it locally

```bash
docker compose -f deploy/docker/docker-compose.dev.yml up -d   # postgres + redis

cd backend
uv sync --extra dev
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 28100   # the API
uv run python -m app.worker                         # the orchestrator

cd ../frontend
npm install
npm run dev                                         # the UI, proxying /api
```

To exercise the whole loop without GPUs, add the mock benchmark platform:

```bash
docker compose -f deploy/docker/docker-compose.dev.yml --profile mock up -d
export AUTOTUNE_LLMBENCH_BASE_URL=http://127.0.0.1:28101
```

and build `mock-engine/` as the campaign's image.

## Before you open a pull request

```bash
cd backend   && uv run ruff check . && uv run pytest -q
cd frontend  && npm run typecheck && npm run build
cd policies/random-search && uv run --extra dev pytest   # a submodule; see below
cd operator  && make test
helm lint deploy/helm/llm-autotune --set jwtSecret=x
```

CI runs all of these. It also fails on any reference to an internal host,
registry or identifier.

Contributions are accepted under the Apache License 2.0, the license this
project is released under.

If you changed anything under `frontend/src/report/`, rebuild the self-contained
report renderer and commit the output:

```bash
cd frontend && npm run build:report      # writes backend/app/agent/static/
```

## How this codebase is written

Two conventions are worth knowing before your first PR, because reviews will
raise them:

- **Comments say why, not what.** A comment that restates the code will be asked
  for or removed. A comment explaining a non-obvious constraint — why a timeout
  is 600 seconds, why a teardown is confirmed rather than assumed — is the point
  of writing one.
- **Tests are named as claims.** `test_a_lingering_container_holds_its_machine_until_the_janitor_confirms_it_gone`,
  not `test_teardown_2`. The suite reads as a description of what the platform
  guarantees.

## Changing the schema

Migrations are Alembic revisions in `backend/alembic/versions/`, incremental on
top of `001_initial`. Generate one against a real Postgres and read what it
produced — autogenerate is a draft, not an answer:

```bash
cd backend && uv run alembic revision --autogenerate -m "what it does"
```

The deployment path is create-or-migrate (`app/db/bootstrap.py`): a fresh
database is created from the models and stamped, an existing one is upgraded. A
migration that only works on one of those paths is a bug.

## Adding a search algorithm

Don't add it to the platform. A search is a container that speaks the
[policy contract](docs/api/policy-contract.md) — fork
[autotune-policy-random-search](https://github.com/modelsphere/autotune-policy-random-search)
and publish your own image. It is a separate repository, checked out here as a
submodule (`git submodule update --init`); changes to a policy are pull requests
against that repository, and this one only moves the pointer.

The SDK under [`policies/autotune_policy/`](policies/autotune_policy/) is the
copy of record. Each policy repository vendors it verbatim — deliberately, so a
policy image needs no package index — so a change here has to be mirrored:
`scripts/sync-policy-sdk.sh` copies it out and CI fails if the copies drift.
The platform deliberately has no plugin point for search logic: keeping the
measurement on one side of an HTTP boundary and the search on the other is what
makes two policies comparable.

## Commit messages and pull requests

Explain the change and why it was needed. A PR that changes behaviour should say
what would have gone wrong without it. Keep unrelated changes in separate PRs.
