"""What an admin set on the Settings page, over the install's env.

Each external platform's address has an env default (the chart's value) and an
optional override in `platform_settings`. Readers ask `resolve()` and never
the env directly, so a value set in the UI takes effect without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import LlmEndpoint, PlatformSetting

VICTORIALOGS_URL = "victorialogs_url"
# A catalog-server's address (model-catalog's catalog-server/): its index is
# read at <url>/index-v2.json, and with a write key the export publishes to
# its api/v2/.
CATALOG_URL = "catalog_url"
CATALOG_TOKEN = "catalog_token"  # secret
REPORTS_LLM_ENDPOINT = "reports_llm_endpoint_id"

KEYS = (VICTORIALOGS_URL, CATALOG_URL, CATALOG_TOKEN, REPORTS_LLM_ENDPOINT)
SECRET_KEYS = (CATALOG_TOKEN,)


@dataclass
class Resolved:
    victorialogs_url: str
    # Where victorialogs_url came from: "settings" | "install" | "".
    victorialogs_source: str
    catalog_url: str  # the catalog-server; "" when none is set
    catalog_index_url: str  # what the export reads models from
    catalog_index_source: str  # "settings" | "install" | ""
    catalog_token: str
    reports_llm_endpoint_id: int | None

    @property
    def catalog_writable(self) -> bool:
        return bool(self.catalog_url and self.catalog_token)


def _clean_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _resolve(rows: dict[str, PlatformSetting]) -> Resolved:
    env = get_settings()

    def value(key: str) -> str:
        row = rows.get(key)
        return (row.value or "").strip() if row is not None else ""

    logs = _clean_url(value(VICTORIALOGS_URL))
    env_logs = _clean_url(env.llmbench_victorialogs_url)
    catalog = _clean_url(value(CATALOG_URL))
    env_index = (env.catalog_index_url or "").strip()
    token_row = rows.get(CATALOG_TOKEN)
    endpoint = value(REPORTS_LLM_ENDPOINT)
    return Resolved(
        victorialogs_url=logs or env_logs,
        victorialogs_source="settings" if logs else ("install" if env_logs else ""),
        catalog_url=catalog,
        catalog_index_url=f"{catalog}/index-v2.json" if catalog else env_index,
        catalog_index_source="settings" if catalog else ("install" if env_index else ""),
        catalog_token=decrypt_secret(token_row.secret) if token_row is not None else "",
        reports_llm_endpoint_id=int(endpoint) if endpoint.isdigit() else None,
    )


async def resolve(db: AsyncSession) -> Resolved:
    rows = {r.key: r for r in (await db.execute(select(PlatformSetting))).scalars()}
    return _resolve(rows)


def resolve_sync(db: Session) -> Resolved:
    rows = {r.key: r for r in db.execute(select(PlatformSetting)).scalars()}
    return _resolve(rows)


async def put(db: AsyncSession, key: str, value: str, user_id: int | None) -> None:
    """Set one key; "" clears it, back to the install's value. Not committed."""
    if key not in KEYS:
        raise KeyError(key)
    row = await db.get(PlatformSetting, key)
    if row is None:
        row = PlatformSetting(key=key, value="", secret="")
        db.add(row)
    if key in SECRET_KEYS:
        row.secret = encrypt_secret(value) if value else ""
        row.value = ""
    else:
        row.value = value.strip()
    row.updated_by_id = user_id


@dataclass
class LlmTarget:
    """An endpoint as a caller needs it: where, which model, and the key."""

    endpoint_id: int
    name: str
    base_url: str
    model: str
    api_key: str


def target_of(row: LlmEndpoint) -> LlmTarget:
    return LlmTarget(
        endpoint_id=row.id,
        name=row.name,
        base_url=_clean_url(row.base_url),
        model=row.model,
        api_key=row.api_key_value(),
    )


async def reports_llm(db: AsyncSession) -> LlmTarget | None:
    """The endpoint report writing uses; None when the admin chose none."""
    chosen = (await resolve(db)).reports_llm_endpoint_id
    row = await db.get(LlmEndpoint, chosen) if chosen is not None else None
    return target_of(row) if row is not None else None
