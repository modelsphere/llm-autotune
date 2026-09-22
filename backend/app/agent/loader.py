"""Load everything the parts of one run are built from, in one place.

A run's context is spread over five tables: the run, its candidate and
campaign, the machine it ran on, and its benchmark result. `RunBundle` is
those rows, loaded once; the part builders are pure functions over it and
never touch the session.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Candidate, Machine, Result, Run


@dataclass
class RunBundle:
    run: Run
    candidate: Candidate | None
    campaign: Campaign | None
    machine: Machine | None
    result: Result | None  # latest llmbench result, if any


async def load_run(session: AsyncSession, run_id: int) -> RunBundle | None:
    run = await session.get(Run, run_id)
    if run is None:
        return None
    candidate = await session.get(Candidate, run.candidate_id) if run.candidate_id else None
    campaign = await session.get(Campaign, run.campaign_id) if run.campaign_id else None
    machine = await session.get(Machine, run.machine_id) if run.machine_id else None
    result = (
        await session.execute(
            select(Result)
            .where(Result.run_id == run.id, Result.source == "llmbench")
            .order_by(Result.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return RunBundle(
        run=run, candidate=candidate, campaign=campaign, machine=machine, result=result
    )
