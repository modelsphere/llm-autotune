# Plugins

A plugin adds to LLM AutoTune without changing it: its own API routes, work
the worker does every tick, tables of its own, and planners. It is a Python
package installed into the platform's environment, kept in its own
repository, and upgraded independently of the platform.

For a new search strategy, a [policy](api/policy-contract.md) is usually
the better fit. A policy is a container the platform talks to over HTTP: it
needs no Python inside the platform, can be written in any language, and
can be registered without a redeploy. A plugin planner runs in the worker's
own process and reads the campaign's history directly. It's the right
choice when the strategy needs no container at all, or must be there for
every campaign of a deployment.

The [example plugin](../backend/tests/plugins/example) uses every hook. It is
the place to start: copy it and rename it.

## What a plugin is

A package that declares a `Plugin` under the entry-point group
`llm_autotune.plugins`:

```toml
# pyproject.toml
[project.entry-points."llm_autotune.plugins"]
example = "example_plugin:plugin"
```

```python
# example_plugin/__init__.py
from pathlib import Path

from app.plugin_api import PLUGIN_API_VERSION, Plugin

plugin = Plugin(
    name="example",
    api_version=PLUGIN_API_VERSION,
    routers=(router,),
    tick_steps=(note_active_campaigns,),
    migrations=str(Path(__file__).parent / "migrations"),
    on_bootstrap=seed,
)
```

Every hook is optional:

| hook | what the platform does with it |
|---|---|
| `routers` | mounts each FastAPI router under `/api`, after the platform's own routes, so a plugin cannot shadow one |
| `tick_steps` | calls each `step(supervisor, session)` at the start of every worker tick, so whatever a step starts is planned, scheduled and advanced in the same tick. Each step runs in a savepoint: one that raises is logged and its writes rolled back, and the tick goes on |
| `migrations` | upgrades the plugin's own Alembic directory to head on every deploy, after the platform's schema and before seeding. The worker waits for it as it waits for the platform's |
| `on_bootstrap(engine)` | runs after the platform's own seeding on every deploy. Like that seeding, it should only add what is missing; if it fails, the failure is logged and the deploy goes on |
| `propose_candidates(ctx)` | plans a campaign in-process, in place of the default enumeration of its space. See [Planning](#planning) |

`name` is lowercase letters, digits and underscores, and must equal the
entry-point name.

## Planning

Each tick, for every ACTIVE campaign that has no policy container, the
platform asks each plugin's `propose_candidates` in turn. The first to
return something other than `None` plans that campaign; if none does, the
platform enumerates the campaign's space as usual. A plugin returns `None`
for a campaign it does not plan, so it decides which campaigns are its own,
typically from its own side table.

The `PlanContext` it receives carries:
- `session` and `campaign`;
- `history()`: every run and queued or rejected candidate as a `RunRecord`,
  with the campaign's objective already applied (its value, whether it was
  feasible, and redline slacks), so every planner ranks on the number the
  report shows;
- `queued()` and `startable_slots()`, to size a batch: an adaptive planner
  should ask for about as many points as can start, not commit to a hundred
  guesses before any result has come back;
- `batch_cap`, the cap for a space whose points do not depend on results.

What it returns is validated and deduplicated by the platform exactly like
an enumerated point: a duplicate is dropped, and a configuration that cannot
fit is recorded as rejected. A planner that raises is logged, and the
campaign gets no new candidates that tick. It does not fall back to
enumeration, which would queue every point of a space meant to be searched
selectively.

The search-space helpers (`expand`, `axes`, `grid_values`, `range_specs`,
`tied_groups`, `prune_inactive`) and the objective's `direction` are in
`app.plugin_api` too.

## Turning one on

Installed is not enabled: set `AUTOTUNE_PLUGINS` to a comma-separated list of
names. With the Helm chart:

```yaml
extraEnv:
  - {name: AUTOTUNE_PLUGINS, value: example}
```

A listed plugin that is not installed, does not load, or was written for
another plugin API version stops the API, the worker and the migrate job at
startup, with a message that says which.

To put a plugin into the image, build on top of the platform's:

```dockerfile
FROM docker.io/4pdosc/llm-autotune-backend:0.2.0
COPY my-plugin /plugins/my-plugin
RUN uv pip install --python /app/.venv/bin/python /plugins/my-plugin
```

The API, worker and migrate job all run from this one image, so all three
get the plugin.

## What a plugin may use

`app.plugin_api` and nothing else. It re-exports the hook types, the models a
plugin reads (`Campaign`, `Run`, `Machine`, `User`…), the database sessions,
the login dependencies, and `migration_env`. Every other module in `app` is
private to the platform and can change in any release.

The example's `pyproject.toml` enforces this with ruff's `banned-api`. Copy
that section into yours, so an import from anywhere else fails your own lint.

`PLUGIN_API_VERSION` changes only when a change would break a plugin written
for the previous version. Additions don't change it. Each such change is
listed under "Plugin API" in the [CHANGELOG](../CHANGELOG.md), and a hook
being replaced keeps working for one release first.

## Data

- **Separate metadata.** A plugin's tables live on its own SQLAlchemy
  metadata (its own `DeclarativeBase`), never on the platform's.
- **Separate migration history.** The plugin has its own Alembic directory,
  whose `env.py` is one call:

  ```python
  from app.plugin_api import migration_env
  from example_plugin.models import Base

  migration_env(Base.metadata, "example")
  ```

  Its revisions are recorded in `alembic_version_<name>`, so the platform's
  history and each plugin's advance independently, and each history's
  `alembic check` compares only its own tables.
- **Data about a platform row goes in a side table.** Key the table by the
  platform row's primary key (`ForeignKey(Campaign.id)`). Never add a column
  to a platform table.
- **Foreign keys point only at primary keys** of the platform's tables. The
  platform treats changing those as a breaking change.

## Testing a plugin

- **The platform's CI** installs the example plugin and runs it against every
  change to the platform, with Postgres. A change that would break a plugin
  fails there first.
- **Your own CI** should do the same against the platform version you
  deploy, and preferably also against `main`, to hear about a coming change
  before a release.
