"""Replay datasets: samples of production traffic that a campaign's Replay
workload sends again, collected by LLMBench from the gateway's request logs in
VictoriaLogs.

A dataset is a collection profile on LLMBench, read from there on every call
rather than mirrored. The form asks for the model, the nodes, and how much and
how often (app/datasets/editor.py has the rest of the defaults). This platform
keeps only who set a dataset up and how often it takes a new sample
(ReplayDataset), and takes those samples itself (app/datasets/refresh.py).
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import platform_settings
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.datasets import editor, refresh
from app.datasets.editor import DatasetForm
from app.datasets.profiles import BuildInFlight, DatasetProfileClient, DatasetProfileError
from app.db.base import get_async_session
from app.db.models import Campaign, CampaignStatus, Event, ReplayDataset, User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datasets", tags=["datasets"])

# A check scans the log store; a day of traffic is plenty to tell whether the
# filters match, whatever window the dataset samples.
CHECK_HOURS = 24


class ValuesIn(BaseModel):
    field: Literal["model", "forwarded_to"]
    # With `forwarded_to`: the backends serving this model.
    model: str = ""
    source_url: str = ""
    hours: float = Field(24.0, gt=0, le=24 * 7)


async def _log_store(db: AsyncSession, form_url: str = "") -> str:
    """The form's address, else Settings → External platforms, else the install's."""
    if form_url.strip():
        return form_url.strip().rstrip("/")
    return (await platform_settings.resolve(db)).victorialogs_url


async def _require_log_store(db: AsyncSession, form_url: str) -> str:
    url = await _log_store(db, form_url)
    if not url:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No VictoriaLogs address: enter it under Advanced, set it in Settings, or "
            "set llmbench.victoriaLogsUrl when installing.",
        )
    return url


async def _call(fn):
    """One LLMBench round trip off the event loop; its refusals become 502s
    that carry LLMBench's own reason."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except HTTPException:
        raise
    except DatasetProfileError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not reach the benchmark platform: {exc}"
        ) from exc


def _find(client: DatasetProfileClient, name: str) -> dict[str, Any]:
    profile = client.find(name)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no dataset named {name!r}")
    return profile


def _may_edit(user: User, row: ReplayDataset | None) -> bool:
    """Its creator or an admin. One this platform's account owns on LLMBench
    but nobody set up here (made through the API) is an admin's."""
    if user.role == UserRole.ADMIN.value:
        return True
    return row is not None and row.created_by_id == user.id


def _campaign_ref(c: Campaign) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "status": c.status}


def _out(
    profile: dict[str, Any],
    row: ReplayDataset | None,
    account: int | None,
    campaigns: list[Campaign],
    user: User,
    now: datetime,
) -> dict[str, Any]:
    name = profile.get("name") or ""
    current = profile.get("current") or None
    last = profile.get("last_build") or None
    owned = account is not None and profile.get("created_by_user_id") == account
    used = [c for c in campaigns if c.dataset_profile == name]
    held = [
        c for c in used
        if current and c.status != CampaignStatus.DONE.value
        and c.dataset_build_id == current.get("build_id")
    ]
    refresh_hours = row.refresh_hours if row is not None else 0
    due = refresh.due_at(refresh_hours, profile, now) if owned else None
    return {
        **editor.form_of(profile, refresh_hours),
        "id": profile.get("id"),
        # Set up on LLMBench rather than here: shown, not edited.
        "external": not owned,
        "can_edit": owned and _may_edit(user, row),
        # LLMBench's own schedule, for one set up there; 0 for ours.
        "schedule_hours": profile.get("schedule_interval_hours") or 0,
        "source_type": profile.get("source_type") or "",
        "enabled": bool(profile.get("enabled", True)),
        "current": current and {
            key: current.get(key)
            for key in ("build_id", "records", "bytes", "built_at", "age_hours",
                        "window_start", "window_end", "buckets", "summary")
        },
        "last_build": last and {
            key: last.get(key)
            for key in ("id", "status", "progress", "error", "records",
                        "created_at", "started_at", "finished_at")
        },
        "used_by": [_campaign_ref(c) for c in used],
        "held_by": [_campaign_ref(c) for c in held],
        "next_refresh_at": due.isoformat() if due else None,
    }


@router.get("", summary="Replay datasets, with their current sample")
async def list_datasets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Every collection profile on LLMBench: the ones set up here, editable by
    their creator, and the ones set up on LLMBench, shown read-only. `collector`
    is false when LLMBench has no collector, so nothing can be sampled."""

    def _read():
        client = DatasetProfileClient()
        profiles, collector = client.list_with_collector()
        return profiles, collector, client.account_id()

    profiles, collector, account = await _call(_read)
    rows = {r.name: r for r in (await db.execute(select(ReplayDataset))).scalars()}
    campaigns = list(
        (await db.execute(select(Campaign).where(Campaign.dataset_profile != ""))).scalars()
    )
    now = datetime.now(UTC)
    settings = get_settings()
    web = (settings.llmbench_web_url or "").rstrip("/")
    return {
        "datasets": [_out(p, rows.get(p.get("name")), account, campaigns, user, now)
                     for p in profiles],
        "collector": collector,
        "victorialogs_url": await _log_store(db),
        "llmbench_url": web,
    }


_FROZEN_FIELDS = ("name", "records", "bytes", "sha256", "frozen_at", "source_profile",
                  "source_build_id", "window_start", "window_end", "origin", "used_by")


@router.get("/frozen", summary="Frozen datasets on LLMBench")
async def frozen_datasets(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """LLMBench's frozen datasets: files that never change, frozen from a build
    there or imported from another installation (an import is always frozen).
    A replay workload can name one, and every run of every campaign that does
    replays exactly the same requests, with nothing to pin. Imported and
    managed on LLMBench (`llmbench_url`); `campaigns` are the ones here that
    replay each, `used_by` the LLMBench benchmarks that do."""
    datasets = await _call(lambda: DatasetProfileClient().frozen())
    campaigns = list(
        (await db.execute(select(Campaign).where(Campaign.dataset_frozen != ""))).scalars()
    )
    web = (get_settings().llmbench_web_url or "").rstrip("/")
    return {
        "frozen": [
            {
                **{k: d.get(k) for k in _FROZEN_FIELDS},
                "campaigns": [
                    {"id": c.id, "name": c.name, "status": c.status}
                    for c in campaigns if c.dataset_frozen == d.get("name")
                ],
            }
            for d in datasets
        ],
        "llmbench_url": web,
    }


@router.post("/check", summary="Count what an unsaved dataset's filters match")
async def check(
    form: DatasetForm,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """How many requests in the last day match the form's model, nodes and
    filters. A failure comes back as `error`, for the form to show."""
    url = await _require_log_store(db, form.source_url)
    hours = min(form.window_hours, CHECK_HOURS)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: DatasetProfileClient().probe(editor.filters_of(form, url, hours))
        )
    except Exception as exc:  # shown in the form, not raised
        return {"matched": 0, "hours": hours, "error": str(exc)}
    return {
        "matched": result.get("matched", 0),
        "hours": hours,
        "error": result.get("error"),
        "sample_prompt_tokens": result.get("sample_prompt_tokens"),
        "query": result.get("query", ""),
    }


@router.post("/values", summary="Models or nodes the log store has seen")
async def values(
    body: ValuesIn,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The models in the gateway's traffic, or the nodes serving one model,
    most requests first: what the form offers instead of asking for names.
    `victorialogs_url` is the install's address, which the form pre-fills."""
    default = await _log_store(db)
    url = await _log_store(db, body.source_url)
    if not url:
        return {"values": [], "error": "no VictoriaLogs address is configured",
                "victorialogs_url": default}
    request = {
        "source_type": editor.VICTORIALOGS,
        "source_url": url,
        "field": body.field,
        "models": [body.model] if body.field == "forwarded_to" and body.model else [],
        "hours": body.hours,
    }
    try:
        result = await anyio.to_thread.run_sync(lambda: DatasetProfileClient().values(request))
    except Exception as exc:
        return {"values": [], "error": str(exc), "victorialogs_url": default}
    return {"values": result.get("values") or [], "error": result.get("error"),
            "victorialogs_url": default}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Set up a replay dataset")
async def create_dataset(
    form: DatasetForm,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Creates the collection profile on LLMBench and asks for its first
    sample. `collect_error` says why that could not start, if it could not."""
    url = await _require_log_store(db, form.source_url)
    if form.name and not editor.valid_slug(form.name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the ID is lowercase letters, digits, '-' and '_', starting with a letter or digit",
        )

    def _create() -> tuple[dict[str, Any], str]:
        client = DatasetProfileClient()
        profiles, _ = client.list_with_collector()
        taken = {p.get("name") for p in profiles}
        if form.name and form.name in taken:
            raise HTTPException(status.HTTP_409_CONFLICT, f"a dataset named {form.name!r} exists")
        name = form.name or editor.unused_slug(
            editor.slug_for(form.display_name or form.model), taken
        )
        profile = client.create({"name": name, **editor.profile_fields(form, url)})
        try:
            client.trigger_build(int(profile["id"]))
            return profile, ""
        except BuildInFlight:
            return profile, ""
        except DatasetProfileError as exc:
            return profile, str(exc)

    profile, collect_error = await _call(_create)
    db.add(ReplayDataset(name=profile["name"], refresh_hours=form.refresh_hours,
                         created_by_id=user.id))
    db.add(Event(actor=user.username, kind="dataset_created",
                 payload={"dataset": profile["name"], "model": form.model}))
    await db.commit()
    return {"name": profile["name"], "display_name": profile.get("display_name") or "",
            "collect_error": collect_error}


@router.put("/{name}", summary="Change a replay dataset")
async def update_dataset(
    name: str,
    form: DatasetForm,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The new settings apply from the next sample; the current one stays."""
    row = await db.get(ReplayDataset, name)
    if not _may_edit(user, row):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only its creator or an admin may change it")
    url = await _require_log_store(db, form.source_url)

    def _save():
        client = DatasetProfileClient()
        profile = _find(client, name)
        return client.update(int(profile["id"]), editor.profile_fields(form, url))

    await _call(_save)
    if row is None:
        row = ReplayDataset(name=name, created_by_id=None)
        db.add(row)
    row.refresh_hours = form.refresh_hours
    await db.commit()
    return {"name": name}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a replay dataset")
async def delete_dataset(
    name: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Refused while an unfinished campaign replays it. The samples already
    collected stay on LLMBench's volume, as LLMBench leaves them."""
    row = await db.get(ReplayDataset, name)
    if not _may_edit(user, row):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only its creator or an admin may delete it")
    users = (
        await db.execute(
            select(Campaign).where(
                Campaign.dataset_profile == name,
                Campaign.status != CampaignStatus.DONE.value,
            )
        )
    ).scalars().all()
    if users:
        names = ", ".join(c.name for c in users)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"used by {names}; finish or delete those campaigns first",
        )

    def _delete():
        client = DatasetProfileClient()
        profile = client.find(name)
        if profile is not None:
            client.delete(int(profile["id"]))

    await _call(_delete)
    if row is not None:
        await db.delete(row)
    db.add(Event(actor=user.username, kind="dataset_deleted", payload={"dataset": name}))
    await db.commit()


@router.post("/{name}/collect", summary="Take a new sample now")
async def collect(
    name: str,
    force: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Refused (409) while an unfinished campaign is measuring on the current
    sample, unless `force`: its later replay runs would use the new sample and
    no longer compare with the earlier ones."""
    client = DatasetProfileClient()
    profile = await _call(lambda: _find(client, name))
    current = profile.get("current") or {}
    if current.get("build_id") and not force:
        held = (
            await db.execute(
                select(Campaign).where(
                    Campaign.dataset_profile == name,
                    Campaign.dataset_build_id == current["build_id"],
                    Campaign.status != CampaignStatus.DONE.value,
                )
            )
        ).scalars().all()
        if held:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{', '.join(c.name for c in held)} is measuring on the current sample",
            )

    def _trigger() -> int:
        try:
            return client.trigger_build(int(profile["id"]))
        except BuildInFlight as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "a sample is already being collected"
            ) from exc

    build_row = await _call(_trigger)
    db.add(Event(actor=user.username, kind="dataset_collect_requested",
                 payload={"dataset": name, "build_row": build_row, "forced": force}))
    await db.commit()
    return {"build_row": build_row}


@router.get("/{name}/builds", summary="A dataset's recent samples")
async def builds(name: str, _user: User = Depends(get_current_user)):
    def _read():
        client = DatasetProfileClient()
        return client.builds(int(_find(client, name)["id"]), limit=10)

    rows = await _call(_read)
    return [
        {key: b.get(key) for key in ("id", "build_id", "status", "trigger", "records",
                                      "size_bytes", "window_start", "window_end", "progress",
                                      "error", "created_at", "finished_at")}
        for b in rows
    ]
