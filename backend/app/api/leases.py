"""The machine lease API — the platform's contract with whoever owns the GPUs.

This is the one surface designed to be driven by another system rather than by
a person, so it differs from the rest of the API on purpose:

  * machines are addressed by **name** (`node-24`), not by our row id, because
    the caller knows its own fleet's names and not our database;
  * `POST /machines/lease` is idempotent on that name — it registers a machine
    the first time and re-leases it every time after, so a caller that retries
    or restarts does not need to know which case it is in;
  * ending a lease is **asynchronous**. It returns immediately with what will
    happen; the machine is genuinely free when `readiness` reads `returnable`.

The lifecycle, in full:

    (no lease) --POST /machines/lease--> ACTIVE
        ACTIVE --POST .../lease/end {polite}--> DRAINING  (finish live runs)
        ACTIVE --POST .../lease/end {eager}---> DRAINING  (kill live runs now)
        ACTIVE --lease_due_at passes----------> DRAINING  (polite, automatic)
      DRAINING --runs done, production back---> RELEASED

Whether production is put back before RELEASED is a deployment decision, not a
mode: with `AUTOTUNE_AUTO_RESTORE_PRODUCTION` on, the captured services are
started again and the lease closes only once that lands; with it off (the
default) the box is handed back exactly as we left it and the put-back is the
admin's, recorded as a `production_left_down` event. Neither hand-back mode
changes that — an eager end kills the benchmarks, not the restore. A machine
whose capture was empty has nothing to put back either way.

Read `stage.hand_back` before ending a lease: it is the branch this will take,
in words.
"""

from datetime import UTC, datetime, timedelta

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.orchestrator.lifecycle import (
    describe,
    readiness,
    returnable_at,
)
from app.control.run_nodes import run_ids_on_machine
from app.core.auth import Principal, get_principal
from app.core.config import get_settings
from app.db.base import get_async_session, sync_session_factory
from app.db.models import (
    TERMINAL_RUN_STATES,
    Event,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineState,
    Run,
    RunNode,
)
from app.hardware import coerce_gpu_type

router = APIRouter(prefix="/machines", tags=["machine leases"])


# -- request / response shapes ------------------------------------------------


class LeaseRequest(BaseModel):
    name: str = Field(..., description="Machine name, e.g. `node-24`. The identity the "
                                       "lease is keyed on; reusing it re-leases the "
                                       "same machine.", examples=["node-24"])
    # Every field below is optional and defaults to None rather than to a
    # value. Re-leasing is meant to be safe with a minimal body — but with
    # schema defaults, `{"name": "node-24"}` silently resized an 8-card box that
    # had been registered as 4, because "not sent" and "sent as the default"
    # were the same thing by the time the handler saw them. Omitted now means
    # "leave it as it is"; the defaults apply only when registering.
    host: str | None = Field(None, description="Address the platform will ssh to AND the "
                                               "address the benchmark platform will send "
                                               "traffic to. Must be routable from both. "
                                               "Required when the machine is new; omit to "
                                               "keep the current one.",
                             examples=["198.51.100.24"])
    ssh_user: str | None = Field(None, description="SSH user for launching containers. "
                                                   "Defaults to `root` for a new machine.")
    ssh_port: int | None = Field(None, ge=1, le=65535, description="Defaults to 22.")
    gpu_count: int | None = Field(None, ge=0, le=64, description="Cards the platform may "
                                                                "schedule onto. Defaults to 8.")
    gpu_type: str | None = Field(None, examples=["H100", "A100"])
    notes: str | None = Field(None, description="Free text shown next to the machine in the UI.")
    due_at: datetime | None = Field(
        None,
        description="When the machine is promised back. Reaching it starts a polite "
        "drain automatically. Omit (or pass `null`) for no expiry: the machine is "
        "held until you end the lease yourself.",
        examples=["2026-08-05T00:00:00Z"],
    )
    lease_note: str = Field("", description="Why this lease exists — shows in the audit trail.")

    @field_validator("gpu_type")
    @classmethod
    def _canonical_gpu_type(cls, v: str | None) -> str | None:
        # None = "leave as is"; a value is canonicalized to the controlled
        # vocabulary (unknown -> 422). A k8s lease's type is probed regardless.
        return None if v is None else coerce_gpu_type(v)


class LeaseEndRequest(BaseModel):
    mode: str = Field(
        LeaseEndMode.POLITE.value,
        description=(
            "`polite` — let runs already in flight finish, start no new ones. "
            "Can take up to a campaign's `max_run_minutes`.\n\n"
            "`eager` — kill live runs now and lose their measurements."
        ),
        examples=["polite"],
    )
    deadline_seconds: int | None = Field(
        None,
        ge=0,
        description="Be off the machine by this many seconds from now, whatever it "
        "costs. On a polite end this is the point where waiting turns into killing. "
        "Omit for no hard stop.",
        examples=[1800],
    )
    reason: str = Field("", description="Recorded in the audit trail.")


class LiveRun(BaseModel):
    run_id: int
    campaign_id: int
    status: str
    gpus: list[int]
    started_at: datetime | None


class LeaseStatus(BaseModel):
    machine: str
    host: str
    gpu_count: int
    gpus_in_use: int
    state: str = Field(..., description="`away` | `available` | `reserved` — what the "
                                        "platform is doing with the box.")
    lease_state: str = Field(..., description="`none` | `active` | `draining` | `released`")
    lease_holder: str
    leased_at: datetime | None
    lease_due_at: datetime | None
    lease_end_mode: str
    lease_deadline_at: datetime | None
    lease_released_at: datetime | None
    readiness: str = Field(
        ...,
        description=(
            "`busy` — our runs are on it.\n\n"
            "`idle` — ours, nothing running; work may still be queued.\n\n"
            "`returnable` — production is back up and the machine is yours."
        ),
    )
    returnable_at: datetime | None = Field(
        None,
        description="Worst case for when a polite hand-back completes, from each live "
        "run's own cutoff. Null when nothing is running.",
    )
    production_status: str = Field(
        ..., description="`none` | `captured` | `cleared` | `restored` — what has been "
        "done to the services we were handed.\n\n"
        "`cleared` does NOT by itself mean production is down: a capture that found "
        "nothing running lands here too, with an empty service list. `stage.hand_back` "
        "tells the two apart."
    )
    live_runs: list[LiveRun]
    stage: dict = Field(..., description="Human-readable position in the hand-over "
                                          "sequence: headline, detail, step index, and "
                                          "`hand_back` — what ending the lease does to "
                                          "production on this machine.")


# -- helpers ------------------------------------------------------------------


def _snapshot(machine_id: int) -> dict:
    """Everything the caller needs, read through the supervisor's own predicates.

    Runs on a sync session in a worker thread because those predicates are the
    ones the tick loop uses; a second async implementation of "is this machine
    busy" would be a second answer waiting to disagree with the first.
    """
    with sync_session_factory() as session:
        machine = session.get(Machine, machine_id)
        if machine is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such machine")
        live = list(
            session.scalars(
                select(Run).where(
                    Run.id.in_(run_ids_on_machine(machine.id)),
                    Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
                )
            ).all()
        )
        free_at = returnable_at(session, machine)
        # Cards are read per NODE on this machine. For a gang's worker the run
        # row carries rank 0's cards, so summing the runs would report the
        # master's GPUs on every member and a worker's own cards nowhere.
        gpus_by_run = {
            node.run_id: list(node.gpu_indices or [])
            for node in session.scalars(
                select(RunNode).where(RunNode.machine_id == machine.id)
            ).all()
        }
        settings = get_settings()
        stage = describe(
            session,
            machine,
            settings.default_max_run_minutes,
            auto=settings.auto_baseline_lifecycle,
            auto_restore=settings.auto_restore_production,
        )
        return {
            "machine": machine.name,
            "host": machine.host,
            "gpu_count": machine.gpu_count,
            "gpus_in_use": sum(
                len(gpus_by_run.get(r.id, r.gpu_indices or [])) for r in live
            ),
            "state": machine.state,
            "lease_state": machine.lease_state,
            "lease_holder": machine.lease_holder,
            "leased_at": machine.leased_at,
            "lease_due_at": machine.lease_due_at,
            "lease_end_mode": machine.lease_end_mode,
            "lease_deadline_at": machine.lease_deadline_at,
            "lease_released_at": machine.lease_released_at,
            "readiness": readiness(session, machine),
            "returnable_at": free_at,
            "production_status": machine.baseline_status,
            "live_runs": [
                {
                    "run_id": r.id,
                    "campaign_id": r.campaign_id,
                    "status": r.status,
                    "gpus": gpus_by_run.get(r.id, r.gpu_indices or []),
                    "started_at": r.started_at,
                }
                for r in live
            ],
            "stage": {
                "headline": stage.headline,
                "detail": stage.detail,
                "step": stage.step,
                "state": stage.state,
                # What ending the lease does to production, so a caller can read
                # the consequence before it posts to .../lease/end.
                "hand_back": stage.hand_back.as_dict(),
            },
        }


async def _by_name(name: str, session: AsyncSession) -> Machine:
    machine = (
        await session.execute(select(Machine).where(Machine.name == name))
    ).scalar_one_or_none()
    if machine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no machine named '{name}'")
    return machine


# -- endpoints ----------------------------------------------------------------


@router.post(
    "/lease",
    response_model=LeaseStatus,
    summary="Lease a machine to the platform",
    description=(
        "Hand a GPU box over for a period. Idempotent on `name`: the first call "
        "registers the machine, later calls update its details and start a fresh "
        "lease, so a caller may retry freely.\n\n"
        "The platform will then, on its own: record what production is running, "
        "benchmark it as the night's control, stop it, run experiments, and put it "
        "back. Nothing is stopped until the control benchmark has passed.\n\n"
        "**Refused** while the machine is draining — finish or wait out the "
        "hand-back first, otherwise the new lease would inherit a teardown already "
        "in progress."
    ),
    responses={
        409: {"description": "The machine is mid-hand-back, or a run still holds it."},
        422: {"description": "A new machine was leased without a `host`."},
    },
)
async def lease_machine(
    body: LeaseRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")

    machine = (
        await session.execute(select(Machine).where(Machine.name == name))
    ).scalar_one_or_none()
    created = machine is None

    if created:
        if not (body.host or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"'{name}' is not registered yet, so `host` is required",
            )
        # Registration defaults, applied once. On every later call an omitted
        # field means "leave it alone", not "reset it to this".
        machine = Machine(name=name, ssh_user="root", ssh_port=22, gpu_count=8)
        session.add(machine)
    elif machine.lease_state == LeaseState.DRAINING.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{name}' is being handed back; wait for readiness=returnable then lease again",
        )

    if body.host is not None and body.host.strip():
        machine.host = body.host.strip()
    if body.ssh_user is not None:
        machine.ssh_user = body.ssh_user
    if body.ssh_port is not None:
        machine.ssh_port = body.ssh_port
    if body.gpu_count is not None:
        machine.gpu_count = body.gpu_count
    if body.gpu_type is not None:
        machine.gpu_type = body.gpu_type
    if body.notes is not None:
        machine.notes = body.notes

    # A k8s node-slice states its own capacity: leasing is the moment to read it
    # from the cluster (nodes may have been added, drained or relabelled since).
    # Best-effort — a probe never blocks a lease.
    if machine.driver == "k8s":
        from app.control.launch import MachineInfo, get_driver

        info = MachineInfo.of(machine)
        try:
            probe = await anyio.to_thread.run_sync(get_driver("k8s").probe_capacity, info)
        except Exception:
            probe = {}
        if probe.get("supported") and probe.get("node_count"):
            machine.gpu_count = probe.get("gpu_count", machine.gpu_count)
            if probe.get("gpu_type"):
                machine.gpu_type = probe["gpu_type"]

    now = datetime.now(UTC)
    machine.state = MachineState.AVAILABLE.value
    machine.lease_state = LeaseState.ACTIVE.value
    machine.lease_holder = principal.actor
    machine.lease_note = body.lease_note
    machine.leased_at = now
    # No expiry unless the caller asks for one. A lease with no due date is held
    # until its holder ends it; a forgotten one no longer hands itself back a day
    # later (which is how a campaign's own machine got drained out from under
    # it mid-start). Pass `due_at` to opt into a polite drain at a set time.
    machine.lease_due_at = body.due_at
    machine.lease_end_mode = ""
    machine.lease_deadline_at = None
    machine.lease_released_at = None

    await session.flush()
    session.add(
        Event(
            actor=principal.actor,
            kind="lease_started",
            payload={
                "machine": name,
                "registered": created,
                "due_at": machine.lease_due_at.isoformat() if machine.lease_due_at else None,
                "note": body.lease_note,
            },
        )
    )
    await session.commit()
    return await anyio.to_thread.run_sync(_snapshot, machine.id)


@router.get(
    "/lease",
    response_model=list[LeaseStatus],
    summary="Status of every machine",
    description="One entry per registered machine, leased or not. Poll this to decide "
    "whether a machine can be taken back.",
)
async def list_leases(
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    ids = list(
        (await session.execute(select(Machine.id).order_by(Machine.id))).scalars().all()
    )

    def read() -> list[dict]:
        return [_snapshot(i) for i in ids]

    return await anyio.to_thread.run_sync(read)


@router.get(
    "/{name}/lease",
    response_model=LeaseStatus,
    summary="Status of one machine",
    description="The single call an external scheduler needs: is the machine busy, "
    "idle, or returnable, and when will it be free.",
)
async def lease_status(
    name: str,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    machine = await _by_name(name, session)
    return await anyio.to_thread.run_sync(_snapshot, machine.id)


@router.post(
    "/{name}/lease/end",
    response_model=LeaseStatus,
    summary="End a lease and take the machine back",
    description=(
        "Asynchronous. Returns as soon as the request is recorded; the machine is "
        "actually yours when `readiness` reads `returnable`.\n\n"
        "**polite** — no new runs start; the ones in flight finish. Check "
        "`returnable_at` for the worst case.\n\n"
        "**eager** — live runs are killed on the next worker tick (~10s) and their "
        "measurements are lost.\n\n"
        "What happens to production afterwards is the same for both modes and is "
        "reported in `stage.hand_back`: restored by us (a further minute or two while "
        "the service loads), left down for the admin to restore, or nothing to do. "
        "`deadline_seconds` bounds the whole thing: past it, a polite end starts "
        "killing too."
    ),
    responses={409: {"description": "The machine has no lease to end."}},
)
async def end_lease(
    name: str,
    body: LeaseEndRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    if body.mode not in (LeaseEndMode.POLITE.value, LeaseEndMode.EAGER.value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mode must be polite|eager")
    machine = await _by_name(name, session)
    if machine.lease_state not in (LeaseState.ACTIVE.value, LeaseState.DRAINING.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{name}' has no active lease (lease_state: {machine.lease_state})",
        )

    # Re-ending an already-draining machine is allowed and is how a caller
    # escalates: polite first, eager when it turns out they needed it sooner.
    machine.lease_state = LeaseState.DRAINING.value
    machine.lease_end_mode = body.mode
    if body.deadline_seconds is not None:
        machine.lease_deadline_at = datetime.now(UTC) + timedelta(seconds=body.deadline_seconds)

    session.add(
        Event(
            actor=principal.actor,
            kind="lease_end_requested",
            payload={
                "machine": name,
                "mode": body.mode,
                "deadline_seconds": body.deadline_seconds,
                "reason": body.reason,
            },
        )
    )
    await session.commit()
    return await anyio.to_thread.run_sync(_snapshot, machine.id)
