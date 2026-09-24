"""Production reference configs, managed by hand, picked up from capture, or
mirrored from the deploy repo.

A baseline is keyed by what it serves and on what — (served_model_name, engine,
card_type) — not by a machine. Campaigns tuning that combination compare their
candidates against it. It is a whole LaunchConfig (image, weights path, knobs,
env, volumes), so it converts losslessly to and from a
campaign and the deploy repo's file.

The binding endpoints are the repo side: bind a baseline to a file on a
release branch, sync it (platform-owned differences adopted, the rest recorded
as divergences), and resolve each divergence with an explicit decision —
adopt, equivalent, repo-owned, ignored, platform-owned — or a shortcut such as
"ignore image". Nothing is reconciled silently.
"""

from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.control import baseline_repo
from app.control.deploy_format import PRESETS, catalog, get_format
from app.control.deploy_format.policy import add_equivalence, apply_shortcut, set_policy
from app.control.engine_command import parse_launch_command
from app.control.gitlab_client import GitLabClient, GitLabError, GitLabUnavailable
from app.control.launch_config import LaunchConfig
from app.control.search.validation import cards_used
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import Baseline, DeployBinding, Event, User
from app.schemas.core import (
    BaselineImportIn,
    BaselineIn,
    BaselineOut,
    BaselineSyncOut,
    BindingPolicyIn,
    DeployBindingIn,
    DeployBindingOut,
    DivergenceResolveIn,
    EquivalenceIn,
)

router = APIRouter(prefix="/baselines", tags=["baselines"])


def _binding_out(binding: DeployBinding | None) -> DeployBindingOut | None:
    if binding is None:
        return None
    return DeployBindingOut.model_validate(binding).model_copy(
        update={"unresolved": binding.unresolved}
    )


def _out(baseline: Baseline) -> BaselineOut:
    return BaselineOut.model_validate(baseline).model_copy(
        update={
            "cards": cards_used(baseline.engine_args or {}),
            "binding": _binding_out(baseline.binding),
        }
    )


async def _get(session: AsyncSession, baseline_id: int) -> Baseline:
    baseline = (
        await session.execute(
            select(Baseline)
            .options(selectinload(Baseline.binding))
            .where(Baseline.id == baseline_id)
        )
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


# -- catalog / parse ------------------------------------------------------------


@router.get("/formats")
async def deploy_formats(_: User = Depends(get_current_user)):
    """Adapters, presets, policy fields and shortcuts — what the binding
    editor offers. Plus the default project, so binding is one click."""
    return {
        **catalog(),
        "default_project": get_settings().gitlab_project,
        "gitlab_configured": GitLabClient().configured,
    }


@router.get("/branches")
async def deploy_branches(
    project: str = "",
    search: str = "",
    _: User = Depends(get_current_user),
):
    """The deploy repo's branches, so nobody has to type one.

    The repo keeps one release branch per (model x card x engine), which is
    why the branch is a choice at all — the binding names the one a baseline
    mirrors, a campaign may name a different one for its winner. Empty
    `project` means the configured default. Unreachable GitLab is not an
    error here: the caller falls back to a free-text field.
    """
    client = GitLabClient()
    target = (project or get_settings().gitlab_project or "").strip()
    if not client.configured or not target:
        return {"project": target, "branches": [], "error": "GitLab is not configured"}
    try:
        branches = await anyio.to_thread.run_sync(
            lambda: client.list_branches(target, search=search)
        )
    except (GitLabError, GitLabUnavailable) as exc:
        return {"project": target, "branches": [], "error": str(exc)}
    return {"project": target, "branches": branches, "error": ""}


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
                .options(selectinload(Baseline.binding))
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


# -- the deploy-repo binding ---------------------------------------------------


def _format_spec(spec: dict) -> dict:
    """Validate a binding's format spec by resolving it once."""
    try:
        get_format(spec)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if spec.get("preset") and spec["preset"] not in PRESETS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown preset {spec['preset']!r}"
        )
    return dict(spec)


def _preset_path(spec: dict) -> str:
    preset = PRESETS.get(spec.get("preset") or "")
    return (preset or {}).get("path", "") or "config/model.yaml"


@router.put("/{baseline_id}/binding", response_model=BaselineOut)
async def bind_baseline(
    baseline_id: int,
    body: DeployBindingIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Bind (or re-address) this baseline to its deploy-repo file. The config
    is not touched here — run a sync to reconcile."""
    baseline = await _get(session, baseline_id)
    spec = _format_spec(body.format)
    binding = baseline.binding
    if binding is None:
        binding = DeployBinding(created_by=user.id)
        baseline.binding = binding  # through the relationship, so the row in hand sees it
    moved = (binding.project, binding.branch, binding.path) != (
        body.project,
        body.branch,
        body.path,
    )
    binding.project, binding.branch = body.project.strip(), body.branch.strip()
    binding.path = body.path.strip() or _preset_path(spec)
    binding.format = spec
    if body.policy is not None:
        binding.policy = body.policy
    if body.equivalences is not None:
        binding.equivalences = body.equivalences
    if moved:
        # A different file: what was synced and decided no longer applies.
        binding.commit, binding.document, binding.synced_at, binding.divergences = "", "", None, []
    session.add(
        Event(
            actor=user.username,
            kind="baseline_bound",
            payload={
                "id": baseline_id,
                "project": binding.project,
                "branch": binding.branch,
                "path": binding.path,
            },
        )
    )
    await session.commit()
    return _out(await _get(session, baseline_id))


@router.delete("/{baseline_id}/binding", response_model=BaselineOut)
async def unbind_baseline(
    baseline_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    baseline = await _get(session, baseline_id)
    if baseline.binding is not None:
        baseline.binding = None  # delete-orphan cascade removes the row
        session.add(
            Event(actor=user.username, kind="baseline_unbound", payload={"id": baseline_id})
        )
        await session.commit()
    return _out(await _get(session, baseline_id))


def _bound(baseline: Baseline) -> DeployBinding:
    binding = baseline_repo.bound(baseline)
    if binding is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this baseline is not bound to a deploy repo — bind it first (project, branch, file)",
        )
    return binding


def _sync_out(baseline: Baseline, report: baseline_repo.SyncReport) -> BaselineSyncOut:
    return BaselineSyncOut(
        baseline=_out(baseline),
        commit=report.document.commit,
        adopted=report.adopted,
        divergences=report.divergences,
        unresolved=report.unresolved,
        image=report.parsed.image,
        model_path=report.parsed.model_path,
        gpus=report.parsed.gpus,
        gpu_product=report.parsed.gpu_product,
        env=report.parsed.env,
        warnings=report.parsed.warnings,
    )


def _gitlab_http(exc: Exception) -> HTTPException:
    if isinstance(exc, GitLabUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.post("/{baseline_id}/binding/sync", response_model=BaselineSyncOut)
async def sync_baseline(
    baseline_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-read the bound file at branch head and reconcile the row with it
    under the binding's policy. Platform-owned differences are adopted and
    listed; everything else becomes a divergence to resolve."""
    baseline = await _get(session, baseline_id)
    binding = _bound(baseline)
    try:
        report = await anyio.to_thread.run_sync(
            lambda: baseline_repo.sync(baseline, binding, now=datetime.now(UTC))
        )
    except (GitLabError, GitLabUnavailable) as exc:
        raise _gitlab_http(exc) from exc
    session.add(
        Event(
            actor=user.username,
            kind="baseline_synced",
            payload={
                "id": baseline_id,
                "commit": report.document.commit,
                "adopted": [a["key"] for a in report.adopted],
                "unresolved": report.unresolved,
            },
        )
    )
    await session.commit()
    return _sync_out(await _get(session, baseline_id), report)


@router.post("/import", response_model=BaselineSyncOut)
async def import_baseline(
    body: BaselineImportIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Create a baseline FROM its deploy-repo file: the file is the config,
    every field adopted, nothing to reconcile."""
    if await _identity_clash(session, body.served_model_name, body.engine, body.card_type):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a baseline for {body.served_model_name} / {body.engine} / "
            f"{body.card_type or '(any card)'} already exists — bind and sync that one",
        )
    spec = _format_spec(body.format)
    binding = DeployBinding(
        project=body.project.strip(),
        branch=body.branch.strip(),
        path=body.path.strip() or _preset_path(spec),
        format=spec,
        policy=body.policy or {},
        created_by=user.id,
    )
    client = GitLabClient()
    try:
        client.require()
        got = await anyio.to_thread.run_sync(
            lambda: client.get_file(binding.project, binding.path, binding.branch)
        )
    except (GitLabError, GitLabUnavailable) as exc:
        raise _gitlab_http(exc) from exc
    baseline = Baseline(
        served_model_name=body.served_model_name,
        engine=body.engine,
        card_type=body.card_type,
        notes=body.notes,
        created_by=user.id,
    )
    report = baseline_repo.import_config(
        baseline,
        binding,
        baseline_repo.Document(got["content"], got["commit"]),
        now=datetime.now(UTC),
    )
    baseline.served_model_name = body.served_model_name  # identity is the caller's, not the file's
    baseline.binding = binding
    session.add(baseline)
    session.add(
        Event(
            actor=user.username,
            kind="baseline_imported",
            payload={
                "model": body.served_model_name,
                "card_type": body.card_type,
                "branch": binding.branch,
                "commit": got["commit"],
            },
        )
    )
    await session.commit()
    return _sync_out(await _get(session, baseline.id), report)


@router.post("/{baseline_id}/binding/resolve", response_model=BaselineOut)
async def resolve_divergence(
    baseline_id: int,
    body: DivergenceResolveIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Decide one divergence: adopt the repo's value, declare the two values
    equivalent, or set who owns the field/knob (repo / ignore / platform)."""
    baseline = await _get(session, baseline_id)
    binding = _bound(baseline)
    try:
        decided = baseline_repo.resolve(
            baseline,
            binding,
            kind=body.kind,
            key=body.key,
            action=body.action,
            actor=user.username,
            now=datetime.now(UTC),
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    session.add(
        Event(
            actor=user.username,
            kind="baseline_divergence_resolved",
            payload={
                "id": baseline_id,
                "kind": body.kind,
                "key": body.key,
                "action": body.action,
                "ours": decided.get("ours"),
                "theirs": decided.get("theirs"),
            },
        )
    )
    await session.commit()
    return _out(await _get(session, baseline_id))


@router.post("/{baseline_id}/binding/policy", response_model=BaselineOut)
async def edit_policy(
    baseline_id: int,
    body: BindingPolicyIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """One policy edit: a shortcut by name ("ignore_image", …) or one
    field/knob's owner. Divergences already recorded for that key follow."""
    baseline = await _get(session, baseline_id)
    binding = _bound(baseline)
    try:
        if body.shortcut:
            binding.policy = apply_shortcut(binding.policy or {}, body.shortcut)
        else:
            binding.policy = set_policy(
                binding.policy or {}, field=body.field, knob=body.knob, owner=body.owner
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # A divergence whose owner is now explicit is decided by that owner.
    fields, knobs = binding.policy.get("fields") or {}, binding.policy.get("knobs") or {}
    stamp = datetime.now(UTC).isoformat()
    binding.divergences = [
        {
            **d,
            "status": (fields if d.get("kind") == "field" else knobs)[d["key"]],
            "decided_by": user.username,
            "decided_at": stamp,
        }
        if d.get("status") == "unresolved"
        and d.get("key") in (fields if d.get("kind") == "field" else knobs)
        else d
        for d in (binding.divergences or [])
    ]
    session.add(
        Event(
            actor=user.username,
            kind="baseline_policy_edited",
            payload={
                "id": baseline_id,
                "shortcut": body.shortcut,
                "field": body.field,
                "knob": body.knob,
                "owner": body.owner,
            },
        )
    )
    await session.commit()
    return _out(await _get(session, baseline_id))


@router.post("/{baseline_id}/binding/equivalences", response_model=BaselineOut)
async def add_binding_equivalence(
    baseline_id: int,
    body: EquivalenceIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Declare that ours ↔ theirs mean the same thing for a field or knob."""
    baseline = await _get(session, baseline_id)
    binding = _bound(baseline)
    if not body.field and not body.knob:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name a field or a knob")
    binding.equivalences = add_equivalence(
        binding.equivalences or {},
        field=body.field,
        knob=body.knob,
        ours=body.ours,
        theirs=body.theirs,
    )
    session.add(
        Event(
            actor=user.username,
            kind="baseline_equivalence_added",
            payload={
                "id": baseline_id,
                "field": body.field,
                "knob": body.knob,
                "ours": body.ours,
                "theirs": body.theirs,
            },
        )
    )
    await session.commit()
    return _out(await _get(session, baseline_id))
