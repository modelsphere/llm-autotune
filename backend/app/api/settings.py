"""The admin Settings page.

    GET/PUT /settings                    external platforms' addresses, the catalog key,
                                         and the endpoint report writing uses (admin)
    POST    /settings/check              can this platform reach VictoriaLogs / the catalog
    GET     /llm-endpoints               the registered endpoints, without keys (anyone)
    POST    /llm-endpoints               register one (admin)
    PUT     /llm-endpoints/{id}          change one (admin)
    DELETE  /llm-endpoints/{id}          refused while something uses it (admin)
    POST    /llm-endpoints/{id}/test     one short call: the address, model and key work

A value set here wins over the install's env (core/platform_settings.py), so
the chart's values are defaults, not the only way to point at a platform.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import platform_settings as ps
from app.core.auth import get_current_user, require_admin
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import Event, LlmEndpoint, Policy, User
from app.schemas.settings import (
    CheckIn,
    CheckOut,
    LlmEndpointIn,
    LlmEndpointOut,
    LlmTestOut,
    SettingsIn,
    SettingsOut,
)

router = APIRouter(tags=["settings"])

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Long enough for a reasoning model to get past its thinking to a word.
TEST_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _transport() -> httpx.AsyncBaseTransport | None:
    """Tests swap in a mock transport here."""
    return None


async def _out(db: AsyncSession) -> SettingsOut:
    resolved = await ps.resolve(db)
    env = get_settings()
    own_logs = resolved.victorialogs_source == "settings"
    return SettingsOut(
        victorialogs_url=resolved.victorialogs_url if own_logs else "",
        victorialogs_install=(env.llmbench_victorialogs_url or "").strip().rstrip("/"),
        catalog_url=resolved.catalog_url,
        catalog_index_install=(env.catalog_index_url or "").strip(),
        catalog_index_url=resolved.catalog_index_url,
        catalog_has_token=bool(resolved.catalog_token),
        catalog_writable=resolved.catalog_writable,
        reports_llm_endpoint_id=resolved.reports_llm_endpoint_id,
    )


def _check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{url!r} is not an http(s) address"
        )
    return url


@router.get("/settings", response_model=SettingsOut)
async def get_platform_settings(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_async_session)
):
    return await _out(db)


@router.put("/settings", response_model=SettingsOut)
async def put_platform_settings(
    body: SettingsIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    changed: list[str] = []
    if body.victorialogs_url is not None:
        await ps.put(db, ps.VICTORIALOGS_URL, _check_url(body.victorialogs_url), admin.id)
        changed.append(ps.VICTORIALOGS_URL)
    if body.catalog_url is not None:
        await ps.put(db, ps.CATALOG_URL, _check_url(body.catalog_url), admin.id)
        changed.append(ps.CATALOG_URL)
    if body.catalog_token is not None:
        await ps.put(db, ps.CATALOG_TOKEN, body.catalog_token.strip(), admin.id)
        changed.append(ps.CATALOG_TOKEN)
    if body.clear_reports_llm_endpoint:
        await ps.put(db, ps.REPORTS_LLM_ENDPOINT, "", admin.id)
        changed.append(ps.REPORTS_LLM_ENDPOINT)
    elif body.reports_llm_endpoint_id is not None:
        if await db.get(LlmEndpoint, body.reports_llm_endpoint_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"no LLM endpoint {body.reports_llm_endpoint_id}",
            )
        await ps.put(db, ps.REPORTS_LLM_ENDPOINT, str(body.reports_llm_endpoint_id), admin.id)
        changed.append(ps.REPORTS_LLM_ENDPOINT)
    if changed:
        # The keys, never a value: the catalog token is a credential.
        db.add(Event(actor=admin.username, kind="settings_updated", payload={"keys": changed}))
    await db.commit()
    return await _out(db)


async def _check_victorialogs(client: httpx.AsyncClient, url: str) -> CheckOut:
    try:
        resp = await client.get(f"{url}/health")
    except httpx.HTTPError as exc:
        return CheckOut(ok=False, url=url, message=f"unreachable from this platform: {exc}")
    if resp.status_code != 200:
        return CheckOut(ok=False, url=url, message=f"{url}/health answered {resp.status_code}")
    return CheckOut(ok=True, url=url, message="reachable")


async def _check_catalog(client: httpx.AsyncClient, url: str, token: str) -> CheckOut:
    try:
        index = await client.get(f"{url}/index-v2.json")
        index.raise_for_status()
        models = len(index.json().get("models") or [])
    except (httpx.HTTPError, ValueError) as exc:
        return CheckOut(ok=False, url=url, message=f"could not read {url}/index-v2.json: {exc}")
    try:
        caps = (await client.get(f"{url}/api/v2")).json()
        writable = bool(caps.get("write"))
    except (httpx.HTTPError, ValueError):
        writable = False
    if not writable:
        return CheckOut(
            ok=True, url=url, writable=False, models=models,
            message=f"{models} model(s); this server takes no writes (a static catalog), "
            "so the export can read it but not publish to it",
        )
    if not token:
        return CheckOut(
            ok=True, url=url, writable=True, token_ok=None, models=models,
            message=f"{models} model(s); set its write key to publish",
        )
    # A dry-run write to a model no catalog has: 401/403 is the key, anything
    # else means the key got past authentication. Nothing is committed.
    probe = (
        "apiVersion: catalog.swiss/v2\nkind: Variant\nmodel: autotune-key-check\nid: check\n"
        'version: 1.0.0\nengine: sglang\nchart: {name: sglang, version: "1.0.0"}\n'
        "requires: {gpus: 1}\n"
    )
    try:
        resp = await client.put(
            f"{url}/api/v2/models/autotune-key-check/variants/check",
            params={"dryRun": "true"},
            content=probe,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/yaml",
                "If-None-Match": "*",
            },
        )
    except httpx.HTTPError as exc:
        return CheckOut(ok=False, url=url, writable=True, models=models,
                        message=f"the write check failed: {exc}")
    if resp.status_code in (401, 403):
        why = "the key was refused" if resp.status_code == 401 else "the server has writes off"
        return CheckOut(ok=False, url=url, writable=True, token_ok=False, models=models,
                        message=f"{why} ({resp.status_code})")
    return CheckOut(ok=True, url=url, writable=True, token_ok=True, models=models,
                    message=f"{models} model(s); the key is accepted")


@router.post("/settings/check", response_model=CheckOut)
async def check_platform(
    body: CheckIn,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    """Whether this platform (not the browser) reaches the address: the API
    pods are what read VictoriaLogs and write to the catalog."""
    resolved = await ps.resolve(db)
    if body.target == "victorialogs":
        url = _check_url(body.url) or resolved.victorialogs_url
    else:
        url = _check_url(body.url) or resolved.catalog_url
    if not url:
        return CheckOut(ok=False, url="", message="no address is set")
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=_transport()) as client:
        if body.target == "victorialogs":
            return await _check_victorialogs(client, url)
        return await _check_catalog(client, url, body.token.strip() or resolved.catalog_token)


# -- LLM endpoints ------------------------------------------------------------------


async def _used_by(db: AsyncSession) -> dict[int, list[str]]:
    used: dict[int, list[str]] = {}
    reports = (await ps.resolve(db)).reports_llm_endpoint_id
    if reports is not None:
        used.setdefault(reports, []).append("reports")
    for name, endpoint_id in (
        await db.execute(
            select(Policy.name, Policy.llm_endpoint_id).where(Policy.llm_endpoint_id.is_not(None))
        )
    ).all():
        used.setdefault(endpoint_id, []).append(f"policy:{name}")
    return used


def _endpoint_out(row: LlmEndpoint, used: dict[int, list[str]]) -> LlmEndpointOut:
    return LlmEndpointOut(
        id=row.id,
        name=row.name,
        description=row.description or "",
        base_url=row.base_url,
        model=row.model,
        has_api_key=bool(row.api_key),
        used_by=used.get(row.id, []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/llm-endpoints", response_model=list[LlmEndpointOut])
async def list_llm_endpoints(
    _user: User = Depends(get_current_user), db: AsyncSession = Depends(get_async_session)
):
    """Names and addresses for anyone choosing one (a policy's form); never a key."""
    used = await _used_by(db)
    rows = (await db.execute(select(LlmEndpoint).order_by(LlmEndpoint.name))).scalars()
    return [_endpoint_out(r, used) for r in rows]


async def _unique_name(db: AsyncSession, name: str, own_id: int | None = None) -> None:
    other = (
        await db.execute(select(LlmEndpoint).where(LlmEndpoint.name == name))
    ).scalar_one_or_none()
    if other is not None and other.id != own_id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"an endpoint named {name!r} exists")


@router.post("/llm-endpoints", response_model=LlmEndpointOut, status_code=status.HTTP_201_CREATED)
async def create_llm_endpoint(
    body: LlmEndpointIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    name = body.name.strip()
    await _unique_name(db, name)
    row = LlmEndpoint(
        name=name,
        description=body.description.strip(),
        base_url=_check_url(body.base_url),
        model=body.model.strip(),
    )
    row.set_api_key((body.api_key or "").strip())
    db.add(row)
    db.add(Event(actor=admin.username, kind="llm_endpoint_added", payload={"name": name}))
    await db.commit()
    await db.refresh(row)
    return _endpoint_out(row, await _used_by(db))


@router.put("/llm-endpoints/{endpoint_id}", response_model=LlmEndpointOut)
async def update_llm_endpoint(
    endpoint_id: int,
    body: LlmEndpointIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    row = await db.get(LlmEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no LLM endpoint {endpoint_id}")
    name = body.name.strip()
    await _unique_name(db, name, endpoint_id)
    row.name = name
    row.description = body.description.strip()
    row.base_url = _check_url(body.base_url)
    row.model = body.model.strip()
    if body.api_key is not None:
        row.set_api_key(body.api_key.strip())
    db.add(Event(actor=admin.username, kind="llm_endpoint_updated", payload={"name": name}))
    await db.commit()
    await db.refresh(row)
    return _endpoint_out(row, await _used_by(db))


@router.delete("/llm-endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_endpoint(
    endpoint_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    row = await db.get(LlmEndpoint, endpoint_id)
    if row is None:
        return
    used = (await _used_by(db)).get(endpoint_id)
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{row.name} is in use ({', '.join(used)}): choose another endpoint there first",
        )
    await db.delete(row)
    db.add(Event(actor=admin.username, kind="llm_endpoint_deleted", payload={"name": row.name}))
    await db.commit()


@router.post("/llm-endpoints/{endpoint_id}/test", response_model=LlmTestOut)
async def test_llm_endpoint(
    endpoint_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
):
    """One short chat completion from this platform, the way report writing
    and agent policies call it: POST <base_url>/chat/completions."""
    row = await db.get(LlmEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no LLM endpoint {endpoint_id}")
    target = ps.target_of(row)
    url = target.base_url
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}
    body = {
        "model": target.model,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "max_tokens": 512,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT, transport=_transport()) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return LlmTestOut(ok=False, message=f"unreachable from this platform: {exc}")
    seconds = round(time.monotonic() - started, 2)
    if resp.status_code != 200:
        text = resp.text[:300].strip()
        if target.api_key:
            text = text.replace(target.api_key, "[key]")
        return LlmTestOut(ok=False, seconds=seconds,
                          message=f"{url} answered {resp.status_code}: {text}")
    try:
        choice = (resp.json().get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reply = str(message.get("content") or message.get("reasoning_content") or "")
    except (ValueError, AttributeError, IndexError):
        return LlmTestOut(ok=False, seconds=seconds, message="the answer is not a chat completion")
    return LlmTestOut(ok=True, seconds=seconds, reply=reply.strip()[:200],
                      message=f"{target.model} answered in {seconds}s")
