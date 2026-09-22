"""The leaderboard ranks on the production-relative ratio when a baseline ran.

Absolute scores drift with the replay dataset night to night; a candidate
expressed as a multiple of the same-night baseline does not. So when a campaign
measured its production baseline, the board orders on that ratio — and says, per
row, whether the candidate beat production.
"""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    Result,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.main import app

_counter = count()
TARGET = "perf_guidellm_sweep.output_tpm_card_norm"
OBJECTIVE = {"target_metric": TARGET, "direction": "maximize", "redlines": []}


@pytest_asyncio.fixture
async def client():
    name = f"lb_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session

    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        await session.commit()
        token = create_token(await session.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.factory = factory
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(factory, rows):
    """rows: list of (kind, score). Each becomes a candidate + succeeded run +
    llmbench result carrying `score` as the objective value."""
    async with factory() as session:
        session.add(
            Campaign(
                id=1, owner_id=1, name="c", engine="sglang", image="img",
                model_path="/m", served_model_name="glm-5", search_space={},
                objective=OBJECTIVE, status=CampaignStatus.ACTIVE.value,
            )
        )
        await session.flush()
        for i, (kind, score) in enumerate(rows, start=1):
            session.add(Candidate(
                id=i, campaign_id=1, config={"tp": 2, "n": i},
                config_hash=f"h{i}", kind=kind, status=CandidateStatus.EXHAUSTED.value,
            ))
            session.add(Run(
                id=i, campaign_id=1, candidate_id=i, machine_id=None,
                kind=RunKind.BASELINE.value if kind == CandidateKind.BASELINE.value
                else RunKind.EXPERIMENT.value,
                status=RunStatus.SUCCEEDED.value,
            ))
            session.add(Result(
                run_id=i, source="llmbench", passed=True, score=score,
                metrics={TARGET: score}, objective_value=score, feasible=True,
                breaches=[],
            ))
        await session.commit()


async def test_the_board_ranks_on_the_baseline_ratio_not_the_raw_score(client):
    # baseline scores 100k; one candidate beats it (120k), one is worse (80k).
    await _seed(client.factory, [
        (CandidateKind.BASELINE.value, 100_000.0),
        (CandidateKind.SEARCH.value, 120_000.0),
        (CandidateKind.SEARCH.value, 80_000.0),
    ])
    board = (await client.get("/api/campaigns/1/leaderboard")).json()

    by_run = {e["run_id"]: e for e in board}
    assert by_run[1]["is_baseline"] and not by_run[2]["is_baseline"]
    # Ratios, direction folded through the objective.
    assert by_run[1]["vs_baseline"] == 1.0
    assert round(by_run[2]["vs_baseline"], 3) == 1.2
    assert round(by_run[3]["vs_baseline"], 3) == 0.8
    # Ranked best-ratio first: winner, then production, then the laggard.
    assert [e["run_id"] for e in board] == [2, 1, 3]


async def test_without_a_baseline_the_ratio_is_absent_and_ranking_is_raw(client):
    await _seed(client.factory, [
        (CandidateKind.SEARCH.value, 80_000.0),
        (CandidateKind.SEARCH.value, 120_000.0),
    ])
    board = (await client.get("/api/campaigns/1/leaderboard")).json()

    assert all(e["vs_baseline"] is None for e in board)
    assert all(not e["is_baseline"] for e in board)
    # Falls back to ranking on the raw score: higher first.
    assert [e["run_id"] for e in board] == [2, 1]


async def _seed_cards(factory, rows):
    """rows: (kind, score, card_type). Same as _seed but stamps each run's
    env_snapshot with the card it landed on."""
    async with factory() as session:
        session.add(Campaign(
            id=1, owner_id=1, name="c", engine="sglang", image="img",
            model_path="/m", served_model_name="glm-5", search_space={},
            objective=OBJECTIVE, status=CampaignStatus.ACTIVE.value,
        ))
        await session.flush()
        for i, (kind, score, card) in enumerate(rows, start=1):
            session.add(Candidate(
                id=i, campaign_id=1, config={"tp": 2, "n": i},
                config_hash=f"h{i}", kind=kind, status=CandidateStatus.EXHAUSTED.value,
            ))
            session.add(Run(
                id=i, campaign_id=1, candidate_id=i, machine_id=None,
                kind=RunKind.BASELINE.value if kind == CandidateKind.BASELINE.value
                else RunKind.EXPERIMENT.value,
                status=RunStatus.SUCCEEDED.value,
                env_snapshot={"card_type": card},
            ))
            session.add(Result(
                run_id=i, source="llmbench", passed=True, score=score,
                metrics={TARGET: score}, objective_value=score, feasible=True,
                breaches=[],
            ))
        await session.commit()


async def test_a_run_on_another_card_is_not_normalized_and_sinks(client):
    # The baseline and one candidate are A100; a second candidate ran on H100
    # and scores higher per card — but H100 throughput is not on the A100 scale.
    await _seed_cards(client.factory, [
        (CandidateKind.BASELINE.value, 100_000.0, "A100"),
        (CandidateKind.SEARCH.value, 120_000.0, "A100"),
        (CandidateKind.SEARCH.value, 200_000.0, "H100"),
    ])
    board = (await client.get("/api/campaigns/1/leaderboard")).json()
    by_run = {e["run_id"]: e for e in board}

    # The A100 candidate is normalized against the A100 baseline.
    assert round(by_run[2]["vs_baseline"], 3) == 1.2
    assert by_run[2]["card_type"] == "A100"
    # The H100 candidate carries NO ratio (different chip) and says which card.
    assert by_run[3]["vs_baseline"] is None
    assert by_run[3]["card_type"] == "H100"
    # And it sinks below the on-chip entries despite the bigger raw number —
    # ranking an off-card run at the top is exactly the mistake this prevents.
    assert [e["run_id"] for e in board] == [2, 1, 3]
