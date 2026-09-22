from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import coerce, params_for
from app.control.search.space import (
    candidate_count,
    expand,
    space_errors,
    swept_keys,
    too_large,
)
from app.control.search.validation import ValidationContext, validate_config
from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import Event, Machine, SearchSpace, User
from app.schemas.core import (
    SearchSpaceCreate,
    SearchSpaceDraft,
    SearchSpaceOut,
    SearchSpacePreview,
)

router = APIRouter(prefix="/search-spaces", tags=["search-spaces"])

_ALLOWED_ENGINES = {"sglang", "vllm"}


def _normalize(body: SearchSpaceDraft) -> dict:
    """The editor's strings, coerced to the catalog's types.

    "2" and 2 render the same flag but hash differently, which would split one
    config into two candidates that look identical in the report.
    """
    engine = body.engine

    def values(key: str, raw: list) -> list:
        return [coerce(engine, key, v) for v in raw]

    return {
        "base": {k: coerce(engine, k, v) for k, v in (body.base or {}).items()},
        "grid": {k: values(k, v) for k, v in (body.grid or {}).items() if v},
        "tied": [
            group
            for group in (
                {k: values(k, v) for k, v in (raw or {}).items() if v}
                for raw in (body.tied or [])
            )
            if group
        ],
        # min/max/step stay numeric regardless of the parameter's own type: a
        # step is arithmetic, not a value the engine ever sees. The values a
        # range produces are coerced by the expander instead, from whether the
        # three bounds are whole numbers.
        "range": {
            k: {b: _number(spec.get(b)) for b in ("min", "max", "step") if b in spec}
            for k, spec in (body.range or {}).items()
            if isinstance(spec, dict)
        },
        "conditions": {
            k: {gate: values(gate, v if isinstance(v, list) else [v])
                for gate, v in (clause or {}).items()}
            for k, clause in (body.conditions or {}).items()
            if isinstance(clause, dict)
        },
    }


def _number(raw):
    """A bound the editor sent as text. Left alone when it is not a number, so
    space_errors reports it rather than this silently dropping it."""
    if isinstance(raw, bool) or raw is None:
        return raw
    try:
        text = str(raw).strip()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except (TypeError, ValueError):
        return raw


def _reject_bad_space(space: dict) -> None:
    errors = space_errors(space)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))


@router.get("/catalog")
async def catalog(engine: str = "sglang", _: User = Depends(get_current_user)):
    """Known parameters for an engine, so the editor can offer them."""
    if engine not in _ALLOWED_ENGINES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "engine must be sglang|vllm")
    return {"engine": engine, "parameters": params_for(engine)}


@router.post("/preview", response_model=SearchSpacePreview)
async def preview(
    body: SearchSpaceDraft,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Expand a space without saving it: how many candidates, would any be
    rejected before they ever cost a machine, and is the space itself sound?"""
    space = _normalize(body)
    swept = swept_keys(space)
    errors = space_errors(space)

    gpu_counts = (
        (await session.execute(select(Machine.gpu_count))).scalars().all()
    )
    gpu_count = max(gpu_counts, default=8)

    # Size is checked BEFORE expanding, not after. The editor calls this on
    # every keystroke, and a space with a few interval axes reaches millions of
    # configurations long before anyone finishes typing the third one —
    # enumerating it to discover it is too big would hang the request that was
    # supposed to say so.
    if too_large(space):
        return SearchSpacePreview(
            candidate_count=candidate_count(space),
            invalid=[],
            gpu_count_used=gpu_count,
            errors=errors,
            sample=[],
        )

    candidates = expand(space)
    ctx = ValidationContext(gpu_count=gpu_count, engine=body.engine)
    invalid = []
    for config in candidates:
        error = validate_config(config, ctx)
        if error:
            invalid.append({"config": _swept_view(config, swept), "error": error})
    return SearchSpacePreview(
        candidate_count=candidate_count(space),
        invalid=invalid,
        gpu_count_used=gpu_count,
        errors=errors,
        sample=(
            [_swept_view(c, swept) for c in candidates[:12]] if swept else candidates[:1]
        ),
    )


def _swept_view(config: dict, swept: list[str]) -> dict:
    """The swept parameters this candidate actually carries.

    Not every candidate carries every swept key: a conditional parameter is
    pruned from the candidates whose gate does not match, which is the whole
    point of declaring the condition. Indexing blindly raised a KeyError that
    surfaced as the preview never returning a count — the editor swallows the
    failure and shows nothing, so a 500 here reads as a hang.
    """
    return {k: config[k] for k in swept if k in config} or config


@router.get("", response_model=list[SearchSpaceOut])
async def list_spaces(
    _: User = Depends(get_current_user), session: AsyncSession = Depends(get_async_session)
):
    return (
        (await session.execute(select(SearchSpace).order_by(SearchSpace.id.desc())))
        .scalars()
        .all()
    )


@router.post("", response_model=SearchSpaceOut)
async def create_space(
    body: SearchSpaceCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    if body.engine not in _ALLOWED_ENGINES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "engine must be sglang|vllm")
    exists = (
        await session.execute(select(SearchSpace).where(SearchSpace.name == body.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "a search space with that name exists")

    normalized = _normalize(body)
    _reject_bad_space(normalized)
    space = SearchSpace(
        owner_id=user.id,
        name=body.name,
        engine=body.engine,
        description=body.description,
        base=normalized["base"],
        grid=normalized["grid"],
        tied=normalized["tied"],
        ranges=normalized["range"],
        conditions=normalized["conditions"],
    )
    session.add(space)
    session.add(
        Event(actor=user.username, kind="search_space_created", payload={"name": body.name})
    )
    await session.commit()
    await session.refresh(space)
    return space


@router.put("/{space_id}", response_model=SearchSpaceOut)
async def update_space(
    space_id: int,
    body: SearchSpaceCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    space = await session.get(SearchSpace, space_id)
    if space is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such search space")
    normalized = _normalize(body)
    _reject_bad_space(normalized)
    space.name, space.engine, space.description = body.name, body.engine, body.description
    space.base, space.grid, space.tied = (
        normalized["base"],
        normalized["grid"],
        normalized["tied"],
    )
    space.ranges, space.conditions = normalized["range"], normalized["conditions"]
    session.add(
        Event(actor=user.username, kind="search_space_updated", payload={"name": body.name})
    )
    await session.commit()
    await session.refresh(space)
    return space


@router.delete("/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(
    space_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    space = await session.get(SearchSpace, space_id)
    if space is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such search space")
    # Campaigns copy the space at creation time, so deleting one never changes
    # a campaign that already ran.
    await session.delete(space)
    session.add(
        Event(actor=user.username, kind="search_space_deleted", payload={"name": space.name})
    )
    await session.commit()
