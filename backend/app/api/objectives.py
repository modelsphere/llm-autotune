from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import Event, Objective, User
from app.metrics_catalog import DEFAULT_TARGET_METRIC, METRICS, default_direction
from app.objective import OPERATORS
from app.schemas.core import ObjectiveCreate, ObjectiveOut

router = APIRouter(prefix="/objectives", tags=["objectives"])


@router.get("/metrics")
async def metrics(_: User = Depends(get_current_user)):
    """Metrics an objective can target or redline, with which direction is
    an improvement — so the editor can default sensibly."""
    return {
        "metrics": METRICS,
        "operators": sorted(OPERATORS),
        "default_target_metric": DEFAULT_TARGET_METRIC,
    }


@router.get("", response_model=list[ObjectiveOut])
async def list_objectives(
    _: User = Depends(get_current_user), session: AsyncSession = Depends(get_async_session)
):
    # Built-ins first: they are the sensible defaults a new user should see.
    return (
        (
            await session.execute(
                select(Objective).order_by(Objective.is_builtin.desc(), Objective.id)
            )
        )
        .scalars()
        .all()
    )


@router.post("", response_model=ObjectiveOut)
async def create_objective(
    body: ObjectiveCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    exists = (
        await session.execute(select(Objective).where(Objective.name == body.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "an objective with that name exists")
    _validate(body)
    objective = Objective(
        owner_id=user.id,
        name=body.name,
        description=body.description,
        target_metric=body.target_metric,
        direction=body.direction or default_direction(body.target_metric),
        redlines=[r.model_dump() for r in body.redlines],
    )
    session.add(objective)
    session.add(
        Event(actor=user.username, kind="objective_created", payload={"name": body.name})
    )
    await session.commit()
    await session.refresh(objective)
    return objective


class DuplicateIn(BaseModel):
    name: str = ""


@router.post("/{objective_id}/duplicate", response_model=ObjectiveOut)
async def duplicate_objective(
    objective_id: int,
    body: DuplicateIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """An editable copy of any objective — the way to start from a built-in one,
    which cannot be edited itself. Named `<name> (copy)` unless a name is given."""
    source = await session.get(Objective, objective_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "objective not found")
    name = body.name.strip() or f"{source.name} (copy)"
    taken = set((await session.execute(select(Objective.name))).scalars())
    if not body.name.strip():
        n = 2
        while name in taken:
            name = f"{source.name} (copy {n})"
            n += 1
    elif name in taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "an objective with that name exists")
    objective = Objective(
        owner_id=user.id, name=name, description=source.description,
        target_metric=source.target_metric, direction=source.direction,
        redlines=list(source.redlines or []),
    )
    session.add(objective)
    session.add(Event(actor=user.username, kind="objective_created",
                      payload={"name": name, "duplicated_from": source.id}))
    await session.commit()
    await session.refresh(objective)
    return objective


@router.put("/{objective_id}", response_model=ObjectiveOut)
async def update_objective(
    objective_id: int,
    body: ObjectiveCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    objective = await session.get(Objective, objective_id)
    if objective is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such objective")
    if objective.is_builtin:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "built-in objectives cannot be edited — duplicate it into your own instead",
        )
    _validate(body)
    objective.name = body.name
    objective.description = body.description
    objective.target_metric = body.target_metric
    objective.direction = body.direction or default_direction(body.target_metric)
    objective.redlines = [r.model_dump() for r in body.redlines]
    session.add(
        Event(actor=user.username, kind="objective_updated", payload={"name": body.name})
    )
    await session.commit()
    await session.refresh(objective)
    return objective


@router.delete("/{objective_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_objective(
    objective_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    objective = await session.get(Objective, objective_id)
    if objective is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such objective")
    if objective.is_builtin:
        raise HTTPException(status.HTTP_409_CONFLICT, "built-in objectives cannot be deleted")
    # Campaigns copy the objective at creation, so this never rewrites history.
    await session.delete(objective)
    session.add(
        Event(actor=user.username, kind="objective_deleted", payload={"name": objective.name})
    )
    await session.commit()


def _validate(body: ObjectiveCreate) -> None:
    if body.direction and body.direction not in ("maximize", "minimize"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "direction must be maximize|minimize"
        )
    for redline in body.redlines:
        if redline.op not in OPERATORS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown operator '{redline.op}' (use {', '.join(sorted(OPERATORS))})",
            )
