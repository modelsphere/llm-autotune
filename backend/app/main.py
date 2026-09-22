import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    agent,
    api_keys,
    auth,
    baselines,
    campaigns,
    clusters,
    leases,
    machine_groups,
    machines,
    objectives,
    policies,
    policy_sessions,
    promotions,
    runs,
    search_spaces,
)
from app.core.config import get_settings

DESCRIPTION = """
Automated parameter search for LLM inference engines (sglang, vLLM).

A **campaign** searches a **search space** against an **objective**, on machines
the platform has been **leased**. Each point becomes a **run**: a container is
launched with those engine arguments, benchmarked by LLMBench, and scored.

### Authentication

Two credentials, both accepted on the lease endpoints:

* **`Authorization: Bearer <jwt>`** — for people. `POST /api/auth/login`.
* **`X-API-Key: atk_…`** — for services. Minted at `POST /api/api-keys`, shown
  once. Everything else takes the bearer token only.

### Integrating a fleet manager

The **machine leases** section is the surface built for external callers. The
short version:

```
POST /api/machines/lease           {"name": "node-24", "host": "…", "due_at": …}
GET  /api/machines/node-24/lease     -> readiness: busy | idle | returnable
POST /api/machines/node-24/lease/end {"mode": "polite"}
```

Ending a lease is asynchronous. The machine is yours when `readiness` reads
`returnable` — by which point the production service we found on it has been
put back and verified.
"""

def _cors_origins() -> list[str]:
    """Browser origins allowed to call this API.

    The default is permissive because the API and the SPA are normally the same
    origin, and a fresh install should not fail with an opaque CORS error. Set
    AUTOTUNE_CORS_ORIGINS to a comma-separated list to tighten it.
    """
    configured = (get_settings().cors_origins or "").strip()
    if not configured or configured == "*":
        return ["*"]
    return [o.strip() for o in configured.split(",") if o.strip()]


TAGS = [
    {
        "name": "machine leases",
        "description": "Hand GPU machines to the platform and take them back. "
        "Designed to be driven by another system: machines are addressed by name, "
        "leasing is idempotent, and both credential types are accepted.",
    },
    {"name": "api keys", "description": "Mint and revoke service credentials."},
    {"name": "campaigns", "description": "Search runs: schedule, candidates, results, reports."},
    {"name": "machines", "description": "Fleet inventory and the production-baseline lifecycle."},
    {"name": "search spaces", "description": "Reusable parameter grids."},
    {"name": "objectives", "description": "What 'better' means: a target metric plus redlines."},
    {"name": "runs", "description": "Individual benchmarked deployments."},
    {"name": "auth", "description": "Login and the current user."},
    {
        "name": "agent",
        "description": "Read models an LLM agent writes performance reports from: runs, "
        "comparisons, saved reports. Contract: docs/api/agent-api.md.",
    },
]


class SpaStaticFiles(StaticFiles):
    """StaticFiles with SPA history-mode fallback: unknown paths (a hard
    refresh on /campaigns, a direct link to /runs) serve index.html instead
    of a bare 404 that looks like a dead platform."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Unknown API routes must stay real 404s, not index.html.
            if exc.status_code == 404 and not path.startswith("api/"):
                return await super().get_response("index.html", scope)
            raise

app = FastAPI(
    title="LLM Autotune",
    version="0.2.0",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    # The interactive docs are the integration guide for the lease API, so they
    # are served from the same origin as the API itself — an external team gets
    # one URL, not a doc site that can drift from the deployment.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

api_prefix = "/api"
app.include_router(auth.router, prefix=api_prefix)
# Before machines: both mount under /machines, and the lease routes use literal
# path segments ("/lease") that must not be swallowed by an id parameter.
app.include_router(leases.router, prefix=api_prefix)
app.include_router(api_keys.router, prefix=api_prefix)
app.include_router(machines.router, prefix=api_prefix)
app.include_router(machine_groups.router, prefix=api_prefix)
app.include_router(clusters.router, prefix=api_prefix)
app.include_router(campaigns.router, prefix=api_prefix)
app.include_router(runs.router, prefix=api_prefix)
app.include_router(search_spaces.router, prefix=api_prefix)
app.include_router(objectives.router, prefix=api_prefix)
app.include_router(baselines.router, prefix=api_prefix)
app.include_router(promotions.router, prefix=api_prefix)
app.include_router(policies.router, prefix=api_prefix)
app.include_router(policy_sessions.router, prefix=api_prefix)
app.include_router(agent.router, prefix=api_prefix)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve the built SPA when configured (routers above win for /api/*).
_static_dir = get_settings().static_dir
if _static_dir and os.path.isdir(_static_dir):
    app.mount("/", SpaStaticFiles(directory=_static_dir, html=True), name="frontend")
