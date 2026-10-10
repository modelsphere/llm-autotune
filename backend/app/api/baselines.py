"""Production reference configs, recorded by hand or from a pasted command.

A baseline is keyed by what it serves and on what — (served_model_name, engine,
card_type) — not by a machine. Campaigns tuning that combination compare their
candidates against it. It is a whole LaunchConfig (image, weights path, knobs,
env, volumes), so it converts losslessly to and from a campaign.
"""


from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.engine_command import parse_launch_command
from app.control.launch_config import LaunchConfig
from app.control.search.validation import cards_used
from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import Baseline, Event, User
from app.schemas.core import BaselineIn, BaselineOut

router = APIRouter(prefix="/baselines", tags=["baselines"])


def _out(baseline: Baseline) -> BaselineOut:
    return BaselineOut.model_validate(baseline).model_copy(
        update={"cards": cards_used(baseline.engine_args or {})}
    )


async def _get(session: AsyncSession, baseline_id: int) -> Baseline:
    baseline = (
        await session.execute(select(Baseline).where(Baseline.id == baseline_id))
    ).scalar_one_or_none()
    if baseline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such baseline")
    return baseline


def _config_of(body: BaselineIn) -> LaunchConfig:
    """The config from the body: explicit fields, filled from a pasted
    production command where they were left blank. Explicit fields win, so
    the UI can show a parsed preview and still let a field be hand-edited."""
    parsed = parse_launch_command(body.command) if body.command.strip() else {}
    return LaunchConfig(
        engine=body.engine,
        image=body.image or parsed.get("image") or "",
        model_path=body.model_path or parsed.get("model_path") or "",
        served_model_name=body.served_model_name,
        service_port=body.service_port or int(parsed.get("service_port") or 0),
        engine_args=dict(body.engine_args)
        if body.engine_args
        else dict(parsed.get("engine_args") or {}),
        extra_env=dict(body.extra_env) if body.extra_env else dict(parsed.get("extra_env") or {}),
        extra_volumes=dict(body.extra_volumes)
        if body.extra_volumes
        else dict(parsed.get("extra_volumes") or {}),
        gpu_type=body.card_type,
    ).normalized()


# -- parse ------------------------------------------------------------


@router.post("/parse")
async def parse_command(body: BaselineIn, _: User = Depends(get_current_user)):
    """Compile a pasted production command — a whole `docker run …` line or a
    bare serve command — into a LaunchConfig, for a live preview in the
    editor. No row is written."""
    parsed = parse_launch_command(body.command)
    config = LaunchConfig.from_parsed(parsed, body.engine)
    return {
        **config.model_dump(),
        "cards": config.cards,
        "warnings": parsed.get("warnings") or [],
        "normalized": parsed.get("normalized") or [],
        "platform_managed": parsed.get("platform_managed") or [],
    }


# -- CRUD -----------------------------------------------------------------------


@router.get("", response_model=list[BaselineOut])
async def list_baselines(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    baselines = (
        (
            await session.execute(
                select(Baseline)
                .order_by(Baseline.served_model_name, Baseline.engine, Baseline.card_type)
            )
        )
        .scalars()
        .all()
    )
    return [_out(b) for b in baselines]


@router.get("/{baseline_id}", response_model=BaselineOut)
async def get_baseline(
    baseline_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    return _out(await _get(session, baseline_id))


@router.get("/{baseline_id}/as-campaign")
async def baseline_as_campaign(
    baseline_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """This baseline as the fields a new campaign starts from —
    the knobs become the search space's fixed base."""
    baseline = await _get(session, baseline_id)
    return LaunchConfig.from_baseline(baseline).as_campaign_fields()


async def _identity_clash(
    session: AsyncSession,
    served_model_name: str,
    engine: str,
    card_type: str,
    exclude_id: int | None = None,
) -> bool:
    existing = (
        await session.execute(
            select(Baseline).where(
                Baseline.served_model_name == served_model_name,
                Baseline.engine == engine,
                Baseline.card_type == card_type,
            )
        )
    ).scalar_one_or_none()
    return existing is not None and existing.id != exclude_id


@router.post("", response_model=BaselineOut)
async def create_baseline(
    body: BaselineIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    if await _identity_clash(session, body.served_model_name, body.engine, body.card_type):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a baseline for {body.served_model_name} / {body.engine} / "
            f"{body.card_type or '(any card)'} already exists — edit that one",
        )
    baseline = Baseline(
        served_model_name=body.served_model_name,
        engine=body.engine,
        card_type=body.card_type,
        source="manual",
        notes=body.notes,
        weights_fingerprint=body.weights_fingerprint,
        created_by=user.id,
    )
    _config_of(body).apply_to_baseline(baseline)
    session.add(baseline)
    session.add(
        Event(
            actor=user.username,
            kind="baseline_created",
            payload={"model": body.served_model_name, "card_type": body.card_type},
        )
    )
    await session.commit()
    return _out(await _get(session, baseline.id))


@router.put("/{baseline_id}", response_model=BaselineOut)
async def update_baseline(
    baseline_id: int,
    body: BaselineIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    baseline = await _get(session, baseline_id)
    if await _identity_clash(
        session, body.served_model_name, body.engine, body.card_type, exclude_id=baseline_id
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "another baseline already has that identity")
    baseline.served_model_name = body.served_model_name
    baseline.card_type = body.card_type
    _config_of(body).apply_to_baseline(baseline)
    baseline.weights_fingerprint = body.weights_fingerprint
    baseline.notes = body.notes
    session.add(
        Event(
            actor=user.username,
            kind="baseline_updated",
            payload={"id": baseline_id, "model": body.served_model_name},
        )
    )
    await session.commit()
    return _out(await _get(session, baseline_id))


@router.delete("/{baseline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_baseline(
    baseline_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    baseline = await _get(session, baseline_id)
    await session.delete(baseline)
    session.add(
        Event(
            actor=user.username,
            kind="baseline_deleted",
            payload={"id": baseline_id, "model": baseline.served_model_name},
        )
    )
    await session.commit()
