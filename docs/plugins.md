# Plugins

A plugin adds to LLM AutoTune without changing it: its own API routes and
pages, work the worker does every tick, tables of its own, and work it runs
on the platform's machines as managed campaigns. It is a Python package
installed into the platform's environment, kept in its own repository, and
upgraded independently of the platform.

A new search strategy is not a plugin: it is a
[policy](api/policy-contract.md), a container the platform talks to over
HTTP. It needs no Python inside the platform, can be written in any
language, and can be registered without a redeploy.

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
| `on_campaign_created(session, campaign, data)` | receives `extensions[<plugin name>]` of a new campaign, in the same transaction; raising `ExtensionRefused` refuses the campaign with that message. See [Campaign extensions](#campaign-extensions) |
| `campaign_extensions(session, campaigns)` | returns `{campaign_id: data}`, shown on the campaign as `extensions[<plugin name>]` |
| `queue_waiters(supervisor, session)` | the plugin's waiters (`QueueWaiter`) in the machine queue, served in arrival order with the platform's own. See [The machine queue](#the-machine-queue) |
| `reservations(session, machine)` | machines the plugin holds (`Reservation`) for a campaign whose run is not placed yet; everyone else treats them as taken |
| `queue_arrival(session, campaign)` | when a campaign joined the queue, for one that stands for an older request |
| `submission_extras(session, run, context)` | fields for the run's LLMBench submission; `contributor` and `source_url` replace the platform's, anything else is added to the body |
| `run_overlay(session, run, campaign)` | what the plugin knows about a run it started for a request of its own, for the run's agent documents (`RunOverlay`, see [Runs in the agent API](#runs-in-the-agent-api)) |
| `run_selector(session, token)` | a run id for an agent API run selector of the plugin's own spelling; raise `SelectorRefused` to answer with a specific error |
| `openapi_tags` | descriptions of the plugin's API tags, shown in the API docs with the platform's |

`name` is lowercase letters, digits and underscores, and must equal the
entry-point name. `requires` names other plugins it builds on (see
[Plugins that build on others](#plugins-that-build-on-others)).

## Managed campaigns

A plugin that needs something measured (one config on one machine, or a
policy searching a space) runs it as a **managed campaign**. It doesn't build
campaign rows or drive the scheduler. It hands the platform a campaign spec,
the same body `POST /api/campaigns` takes and a campaign's YAML export holds,
and the platform runs it like any other campaign: the machine queue, leases,
windows, benchmarks and results are the platform's.

```python
from datetime import timedelta

from app.plugin_api import Owner, create_managed_campaign, stop_managed_campaign

campaign = create_managed_campaign(
    session,
    owner=Owner(plugin="example", label="Request #4", link="/example/requests/4"),
    owner_id=user.id,
    spec={"name": "...", "engine": "sglang", "image": "...", "model_path": "...",
          "served_model_name": "...", "search_space": {"base": {"tp": 2}},
          "benchmark_slug": "..."},
    start_for=timedelta(hours=2),
)
```

- **Same rules as any campaign.** The spec is checked by the rules the API
  applies (`campaign_errors`), and a broken one raises
  `ManagedCampaignRefused` with the reason; nothing is created. A managed
  campaign names its benchmarks by slug (`benchmark_spec` is refused: making
  a benchmark is a call to LLMBench, which does not belong in a tick).
- **Started or not.** `start_for` starts it now and holds it open that long,
  as Force start does. Without it the campaign is created as the API creates
  one (DRAFT, or SCHEDULED when the spec has a nightly window).
- **Its owner is on record.** The campaign list leaves managed campaigns out
  unless asked (`GET /api/campaigns?managed=include|only`, and a switch on
  the Campaigns page when plugins are installed). The campaign's page says
  what it runs for and links to `Owner.link`.
- **Reading progress.** The plugin keeps the campaign id and reads the
  platform's own rows: the campaign's status, its runs and their results.
- **Starting it later.** A campaign created without `start_for` waits until
  `start_managed_campaign(session, campaign, plugin=..., start_for=...,
  machine_names=[...])` starts it, optionally on the machines the plugin
  chose (a plugin that hands out machines one campaign at a time).
- **Moving its end.** `extend_managed_campaign(session, campaign, plugin=...,
  until=...)` moves the end of a running campaign's window, for work whose
  clock should start when it is placed rather than when it was started.
- **One dataset for several.** `pin_managed_dataset(session, campaign,
  plugin=..., build=..., applied=...)` gives a campaign a dataset build
  before it measures, so campaigns that must compare (and are started one
  after another) measure on the same one: a campaign holding a build, started
  or not, keeps it from being rebuilt. `build=None` clears it.
- **Closing it.** `stop_managed_campaign(session, campaign, plugin=...)` shuts
  the window now, so the platform ends whatever is still running the way it
  ends any campaign's runs at the end of a window, and aborts a live policy
  session. It returns True once the campaign is DONE, which is when nothing
  of it is running; call it again on a later tick until then.
- **Only its own.** Each of these refuses (`ManagedCampaignRefused`) a
  campaign the plugin does not own.
- **On the record.** `record_event(session, kind, plugin=..., ...)` adds to
  the platform's event log, which a campaign's Activity tab and the reports
  read. Pass `actor=` (a username) for something a person asked for;
  without it the event is the plugin's own.

## Campaign extensions

A plugin keeps its own fields about a campaign through `extensions`, keyed
by plugin name, on every way a campaign is made and read:

```json
POST /api/campaigns
{"name": "...", "search_space": {...}, "extensions": {"example": {"label": "nightly"}}}
```

- **Creating:** each plugin's `on_campaign_created` gets its part and saves
  it in its own side table. A key that no enabled plugin takes is refused
  with a 422, not dropped: a campaign that runs without what its author asked
  for is worse than one that does not start.
- **Reading:** every endpoint that returns a campaign shows
  `extensions[<plugin name>]`, from `campaign_extensions`. It is asked once
  for a whole list of campaigns. A plugin that fails to read is logged and
  left out.
- **Copying:** `GET /api/campaigns/{id}/spec`, YAML export and clone carry
  the extensions, so a copy is created with them again. A clone can override
  them like any other field.

## The machine queue

The worker hands machines out through one queue, oldest waiter first, one
run per turn: campaigns with a run ready, policy sessions without a machine,
and whatever plugins add. A campaign that places a run goes to the back, so
two campaigns on one machine alternate. A waiter that cannot fit a machine it
could use holds that machine for the rest of the pass, so a wide request is
not starved by a stream of narrow ones.

A plugin joins the queue with `queue_waiters`. Each `QueueWaiter` has an
`arrival` time and a `try_admit(session, blocked, busy)`:
- `blocked` holds the machines older waiters are holding;
- return True once the waiter took what it needed;
- otherwise add to `busy` the machines it could use once they free up, and
  return False.

A waiter that admits a campaign to a machine usually reports that machine
through `reservations` until the campaign's first run is placed. Placement,
policy sessions and every other waiter count a reservation as taken.
`busy_reason(session, machine, cards=, share=)` answers "why can't a run of
this width go here right now" from the same accounting.

## Runs in the agent API

A plugin that starts runs for requests of its own describes them with
`run_overlay`: a `RunOverlay` whose every field is optional, and whatever it
says replaces what the platform would build from its own rows. That covers:
- the launch configuration and how the run is named in a comparison;
- where it came from (`source`, plus `origin` details under
  `launch.origin.extensions`);
- the module verdicts and metrics the plugin froze when it harvested the
  result;
- the SLO, quality floors and ranking metric it holds the run to;
- the benchmark's recorded config hash, so a document says when the
  benchmark has `drifted` since;
- the `group` the run belongs to.

Runs of one group compare by the group's rules: two runs of the same group
are comparable whatever else differs, and runs of two groups are not
(`group_differs`). A saved report records the group of the runs it compares,
and `GET /api/agent/v1/reports?group=<slug>` lists by it. A group with a
`page_path` (its page in the web UI, usually a plugin route) is linked from
the report pages.

## Plugins that build on others

A plugin may build on another: list it in `requires`, and the platform loads,
migrates, mounts and ticks it after the one it requires. The plugin may then
import that plugin's own modules and point foreign keys at its tables. A
requirement that `AUTOTUNE_PLUGINS` does not enable, or two plugins that
require each other, stop startup with a message that says which.

Keep the dependency one-way and narrow (one module the other plugin offers
for the purpose), so either can still be removed in the right order.

## Frontend

A plugin's pages live in a folder compiled into the frontend at build time:
copy it to `frontend/src/plugins/installed/<name>/` before `npm run build`.
Its `index.ts` default-exports a `FrontendPlugin`:

```ts
import type { FrontendPlugin } from '@/plugins/api'

const plugin: FrontendPlugin = {
  name: 'example',
  routes: [{ path: '/example', component: () => import('./TicksView.vue') }],
  nav: [{ path: '/example', label: 'example.nav', group: 'top', order: 45 }],
  messages: { en: { example: { nav: 'Example' } }, zh: { example: { nav: '示例' } } },
  slots: { 'account.cards': AccountCard },
}
export default plugin
```

- **`routes`** are added after the app's own. List a literal path before a
  parameter one (`/things/new` before `/things/:id`).
- **`nav`** entries go under the Tuning menu (`group: 'tuning'`) or on the top
  row (`'top'`), placed by `order` among the app's own entries (10, 20, 30…).
- **`messages`** are merged into the app's dictionaries, so `t('example.nav')`
  works anywhere.
- **`slots`** fill the places the app marks with `<PluginSlot>`:
  - `account.cards`: cards on the account page, with props `me` and `reload`;
  - `campaign-detail.sections`: sections above a campaign's tabs, with props
    `campaign` and `reload`.
A frontend plugin imports from `@/plugins/api` only: the API client, `useI18n`,
`usePoll`, the link and list-filter helpers, the report renderer, and the
building blocks of the platform's own pages, so a plugin's pages look and
behave like them:
- **Lists:** `ListFilters` with `useQueryFilters` and `countOptions`;
- **Loading:** `useFirstLoad` (a page's first load as `loading` and
  `error`, and a table's empty text while it loads), `PageLoading` (a
  spinner, or the error with a retry) and `all` (requests at once, not in a
  row);
- **At a glance:** `FactTile`, `ScoreBars`, `ConfigChips`, `FlagChips`, the
  status helpers (`campaignStatus`, `runStatus`, `failureLabel`);
- **Forms:** `WorkloadPicker`, `NightlyWindow`, `InfoHint`;
- **Runs and results:** `CampaignRuns`, `LogDialog`, `CatalogExportDialog`;
- **Links:** `LinkButton`, `BackLink`.

Keep a plugin's forms to what most people change, and put the rest under an
**Advanced** section (an `el-collapse`), as the platform's own forms do.

CI builds the frontend with `backend/tests/plugins/example/frontend`
installed on every change.

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
FROM docker.io/4pdosc/llm-autotune-backend:0.3.0
COPY my-plugin /plugins/my-plugin
RUN uv pip install --python /app/.venv/bin/python /plugins/my-plugin
```

The API, worker and migrate job all run from this one image, so all three
get the plugin.

## What a plugin may touch

`app.plugin_api` and nothing else. Every other module in `app` is private to
the platform and can change in any release. It offers:
- **the hooks** and their types;
- **managed campaigns**: `create_managed_campaign`, `start_managed_campaign`,
  `extend_managed_campaign`, `pin_managed_dataset`, `stop_managed_campaign`,
  `owners_of`, `record_event`, and the spec type `CampaignCreate`;
- **the platform's tables**, to read: `Campaign`, `Run`, `Result`, `Machine`,
  `Event`, `User`… and their status enums;
- **users, sessions, settings and secrets**: the login dependencies, the
  database sessions, `get_settings`, `encrypt_secret` / `decrypt_secret`;
- **machines and the queue**: lease and availability checks
  (`machine_warnings`, and `preflight.lease_check` to show one), `busy_reason`,
  `reservations_on`, the queue keys, `preflight`;
- **the search space, engines and configs**: `expand`, `space_errors`,
  `cards_used`, `LaunchConfig`, `parse_launch_command`, the flag helpers;
- **measuring**: objectives (`direction`, `target_metric`, `sort_key`),
  `summary_of`, `Gate`, `scenarios`, `LLMBenchClient`, dataset pinning;
- **the agent API's run documents**: `load_run`, the document parts and
  their schemas.

With it come three rules:
- **The platform's tables are read-only to a plugin.** It reads them freely
  and changes them only through the services above: work it needs run is a
  managed campaign, and what it wants on the record goes through
  `record_event`.
- **A plugin's own tables point at the platform's only by primary key** (see
  [Data](#data)).
- **A plugin imports another plugin only if it `requires` it.**

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
