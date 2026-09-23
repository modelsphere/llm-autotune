"""Promotion: expose a winner's exact config and hand it to CICD.

Two layers. The pure one — build the reproducible config payload, and what the
manual/gitlab targets do with it — needs no database. The API flow seeds a
succeeded, benchmarked run and drives POST /promote through to a recorded
Promotion.
"""

from itertools import count

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.control.baseline_repo import Document
from app.control.launch_config import LaunchConfig
from app.control.promotion import PromotionRequest, PromotionUnavailable, get_target
from app.control.promotion.config import build_promotion_config
from app.control.promotion.gitlab import GitLabPromotionTarget
from app.control.promotion.merge_request import Origin, build_merge_request
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Baseline, Campaign, Candidate, DeployBinding, Result, Run, User
from app.main import app

# -- pure config extraction ---------------------------------------------------


def _rows():
    campaign = Campaign(
        id=1,
        owner_id=1,
        name="qwen night",
        engine="sglang",
        image="registry/sglang:latest",
        model_path="/models/qwen",
        served_model_name="glm-5",
        search_space={},
        service_port=28200,
        extra_env={"SGLANG_MOE_CONFIG_DIR": "/moe"},
        extra_volumes={"/moe": "/moe:ro"},
        objective={"target_metric": "output_tpm_card_norm"},
    )
    candidate = Candidate(
        id=1, campaign_id=1, config={"tp": 4, "attention_backend": "fa3"}, config_hash="h"
    )
    run = Run(
        id=214,
        campaign_id=1,
        candidate_id=1,
        status="succeeded",
        env_snapshot={"image_digest": "registry/sglang@sha256:abc123", "engine_version": "0.4"},
    )
    result = Result(
        id=1,
        run_id=214,
        source="llmbench",
        objective_value=1875.0,
        feasible=True,
        breaches=[],
        # Namespaced by module, as every harvested result is.
        metrics={"output_tpm_card_norm": 1875.0, "replay.dataset_id": "20260807T00Z"},
    )
    return campaign, candidate, run, result


def test_config_is_exact_and_reproducible():
    campaign, candidate, run, result = _rows()
    config = build_promotion_config(campaign, candidate, run, result)

    # The tuned knobs, in the snake_case shape the search space speaks.
    assert config["engine_args"] == {"tp": 4, "attention_backend": "fa3"}
    assert config["cards"] == 4  # derived from tp, like a candidate's
    # The image is pinned to the DIGEST the run actually ran, not the tag.
    assert config["image"]["ref"] == "registry/sglang@sha256:abc123"
    assert config["image"]["tag"] == "registry/sglang:latest"
    # The exact server command — the same argv a driver would build.
    assert config["command"][:3] == ["python3", "-m", "sglang.launch_server"]
    assert "--attention-backend" in config["command"]
    # The evidence travels with the config.
    assert config["evidence"]["score"] == 1875.0
    assert config["evidence"]["target_metric"] == "output_tpm_card_norm"
    assert config["evidence"]["dataset_build_id"] == "20260807T00Z"


def test_config_falls_back_to_the_tag_without_a_digest():
    campaign, candidate, run, result = _rows()
    run.env_snapshot = {}  # nothing captured
    config = build_promotion_config(campaign, candidate, run, result)
    assert config["image"]["ref"] == "registry/sglang:latest"


def test_manual_target_renders_an_artifact_and_stays_put():
    campaign, candidate, run, result = _rows()
    config = build_promotion_config(campaign, candidate, run, result)
    target = get_target("manual")
    handle = target.open_rollout(PromotionRequest(config=config, campaign_id=1, run_id=214))
    assert handle.state == "submitted"
    assert "python3" in handle.refs["command"]
    assert handle.refs["image"] == "registry/sglang@sha256:abc123"
    # No external system to poll — status stays where open_rollout left it.
    assert target.status(handle).state == "submitted"


def _bound_baseline(document: str) -> Baseline:
    """A baseline mirroring a deploy file, synced once (offline copy held)."""
    baseline = Baseline(
        id=7,
        served_model_name="glm-5",
        engine="sglang",
        card_type="H100",
        engine_args={"tp": 2, "attention_backend": "triton"},
        image="registry/sglang:latest",
        model_path="/models/qwen",
    )
    baseline.binding = DeployBinding(
        id=1,
        baseline_id=7,
        project="group/deploy",
        branch="release/glm-5-h100",
        path="config/model.yaml",
        format={},
        policy={},
        equivalences={},
        divergences=[],
        commit="abc123",
        document=document,
    )
    return baseline


MODEL_YAML = (
    "image:\n  repository: registry/sglang\n  tag: latest\n\n"
    'model:\n  localPath: /models/qwen\n  gpus: "2"\n\n'
    'extraArgs:\n  - "--tp-size=2"\n  - "--attention-backend=triton"\n'
)


def test_a_winner_becomes_a_merge_request_draft_against_the_bound_file():
    campaign, candidate, run, result = _rows()
    config = build_promotion_config(campaign, candidate, run, result)
    target = LaunchConfig.from_promotion_config(config, gpu_type="H100")
    baseline = _bound_baseline(MODEL_YAML)
    draft = build_merge_request(
        target,
        baseline,
        Origin(kind="campaign", campaign_id=1, campaign_name="qwen night", run_id=214),
        document=Document(MODEL_YAML, "abc123"),
        actor="sun",
        evidence={"target_metric": "output_tpm_card_norm", "score": 1875.0, "vs_baseline": 1.12},
    )
    assert draft.ready
    assert draft.repo_branch == "release/glm-5-h100" and draft.repo_path == "config/model.yaml"
    assert draft.source_branch.startswith("autotune/glm-5-run214-")
    changes = {c["key"]: c for c in draft.plan["changes"]}
    assert changes["tp"]["before"] == "2" and changes["tp"]["after"] == 4
    assert changes["attention_backend"]["after"] == "fa3"
    assert draft.plan["gpus"] == {"before": 2, "after": 4}
    assert '-  - "--tp-size=2"' in draft.diff and '+  - "--tp-size=4"' in draft.diff
    assert "×1.120 of the production baseline" in draft.description
    assert "requested by sun" in draft.description
    # A stale binding (branch moved since the sync) is said out loud.
    stale = build_merge_request(
        target,
        baseline,
        Origin(kind="campaign", campaign_id=1, run_id=214),
        document=Document(MODEL_YAML.replace("triton", "flashinfer"), "def456"),
    )
    assert stale.stale and stale.drift[0]["key"] == "attention_backend"
    assert "the branch is now at `def456`" in stale.description


def test_a_draft_needs_a_bound_baseline():
    campaign, candidate, run, result = _rows()
    target = LaunchConfig.from_promotion_config(
        build_promotion_config(campaign, candidate, run, result)
    )
    origin = Origin(kind="campaign", campaign_id=1, run_id=214)
    assert not build_merge_request(target, None, origin).ready
    unbound = Baseline(id=8, served_model_name="glm-5", engine="sglang", engine_args={})
    unbound.binding = None
    draft = build_merge_request(target, unbound, origin)
    assert not draft.ready and "not bound" in draft.reason


def test_gitlab_dry_run_records_the_draft_without_calling_out():
    campaign, candidate, run, result = _rows()
    config = build_promotion_config(campaign, candidate, run, result)
    target = LaunchConfig.from_promotion_config(config, gpu_type="H100")
    draft = build_merge_request(
        target,
        _bound_baseline(MODEL_YAML),
        Origin(kind="campaign", campaign_id=1, run_id=214),
        document=Document(MODEL_YAML, "abc123"),
    )
    gitlab = GitLabPromotionTarget()  # dry-run by default
    assert gitlab.dry_run
    handle = gitlab.open_rollout(
        PromotionRequest(config=config, campaign_id=1, run_id=214, draft=draft.payload())
    )
    assert handle.state == "submitted"
    assert handle.refs["dry_run"] is True
    assert handle.refs["branch"] == draft.source_branch
    assert handle.refs["target_branch"] == "release/glm-5-h100"
    assert '+  - "--tp-size=4"' in handle.refs["diff"]
    assert "no merge request was created" in handle.detail
    # Without a draft (unbound baseline) the target says so, specifically.
    with pytest.raises(PromotionUnavailable):
        gitlab.open_rollout(PromotionRequest(config=config, campaign_id=1, run_id=214))
    # Status of a dry-run handle stays put.
    assert gitlab.status(handle).state == "submitted"


# -- the API flow -------------------------------------------------------------

_counter = count()


async def _seed(factory, *, feasible=True):
    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        session.add(
            Campaign(
                id=1,
                owner_id=1,
                name="qwen night",
                engine="sglang",
                image="registry/sglang:latest",
                model_path="/models/qwen",
                served_model_name="glm-5",
                search_space={},
                service_port=28200,
                objective={"target_metric": "output_tpm_card_norm"},
            )
        )
        session.add(
            Candidate(
                id=1,
                campaign_id=1,
                config={"tp": 4},
                config_hash="h",
                kind="search",
                status="exhausted",
            )
        )
        session.add(
            Run(
                id=214,
                campaign_id=1,
                candidate_id=1,
                status="succeeded",
                env_snapshot={"image_digest": "registry/sglang@sha256:abc"},
            )
        )
        session.add(
            Result(
                id=1,
                run_id=214,
                source="llmbench",
                objective_value=1875.0 if feasible else 5.0,
                feasible=feasible,
                breaches=[] if feasible else ["output_tpm below 1000"],
                metrics={"output_tpm_card_norm": 1875.0 if feasible else 5.0},
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def client():
    name = f"promo_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    token = None
    await _seed(factory)
    async with factory() as session:
        token = create_token(await session.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.factory = factory
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_promote_an_explicit_run_records_a_manual_rollout(client):
    resp = await client.post("/api/campaigns/1/promote", json={"run_id": 214})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "submitted"
    assert body["target"] == "manual"
    assert body["config"]["cards"] == 4
    assert body["config"]["image"]["ref"] == "registry/sglang@sha256:abc"
    assert "artifact" in body["refs"]

    listed = (await client.get("/api/promotions?campaign_id=1")).json()
    assert [p["id"] for p in listed] == [body["id"]]
    fetched = (await client.get(f"/api/promotions/{body['id']}")).json()
    assert fetched["run_id"] == 214


async def test_promote_without_a_run_picks_the_leaderboard_winner(client):
    resp = await client.post("/api/campaigns/1/promote", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] == 214  # the only successful, feasible run


async def test_refresh_a_manual_promotion_keeps_it_submitted(client):
    created = (await client.post("/api/campaigns/1/promote", json={"run_id": 214})).json()
    refreshed = (await client.post(f"/api/promotions/{created['id']}/refresh")).json()
    assert refreshed["state"] == "submitted"


async def test_promoting_an_unknown_campaign_is_404(client):
    assert (await client.post("/api/campaigns/999/promote", json={})).status_code == 404


async def test_an_infeasible_winner_needs_force(client):
    # Re-seed the run's result as redline-crossing.
    async with client.factory() as session:
        result = await session.get(Result, 1)
        result.objective_value = 5.0
        result.feasible = False
        result.breaches = ["output_tpm below 1000"]
        await session.commit()

    blocked = await client.post("/api/campaigns/1/promote", json={})
    assert blocked.status_code == 409
    assert "redline" in blocked.json()["detail"]

    forced = await client.post("/api/campaigns/1/promote", json={"force": True})
    assert forced.status_code == 200, forced.text
