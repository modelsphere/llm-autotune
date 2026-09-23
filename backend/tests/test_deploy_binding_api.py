"""The deploy-repo binding, end to end through the API: bind a baseline to
its file, sync it against a fake GitLab, resolve what diverges, then turn a
campaign winner into a merge-request preview and a (dry-run) promotion.

GitLab is faked at the client boundary — `get_file` and the two writes — so
the tests exercise the real adapter, policy, sync and draft code with a real
file shape and no network.
"""

from itertools import count

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.control import gitlab_client
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, Candidate, Result, Run, User
from app.main import app

_counter = count()

MODEL_YAML = """\
# Model developer configuration layered on the shared SGLang chart.
image:
  repository: registry.example.com/sglang
  tag: v0.5.15-cu129

model:
  name: kimi
  localPath: /mnt/disk0/models/modelforge/release_260817
  mountPath: /models
  gpus: "2"

service:
  type: ClusterIP
  port: 8050

# The shared chart supplies the launcher, model path, served name, host, port
# and metrics flags. Model developers own the remaining engine arguments.
extraArgs:
  - "--tp-size=2"
  - "--chunked-prefill-size=16384"
  - "--mem-fraction-static=0.85"
  - "--enable-cache-report"
  - "--reasoning-parser=qwen3"
  - "--speculative-draft-model-path=/draft-model"

env:
  - name: NVIDIA_DISABLE_REQUIRE
    value: "1"
"""


class FakeGitLab:
    """The calls the platform makes, with a mutable file and a log.

    `branches` holds the repo's OTHER release branches — the deploy repo keeps
    one per model x card x engine, and their files are different configs, not
    copies. Anything not in it answers with the tracked branch's file.
    """

    def __init__(self, text: str, commit: str = "aaa111"):
        self.text, self.commit = text, commit
        self.configured = True
        self.branches: dict[str, tuple[str, str]] = {}
        self.commits: list[dict] = []
        self.merge_requests: list[dict] = []

    def require(self):
        return None

    def get_file(self, project, path, ref):
        text, commit = self.branches.get(ref, (self.text, self.commit))
        return {"content": text, "commit": commit, "blob_id": "b", "ref": ref}

    def list_branches(self, project, search="", per_page=100):
        names = ["release/modelforge_0.0.2-nvidia_h100-sglang", *self.branches]
        return [
            {"name": n, "default": False, "protected": True, "commit": "aaa111", "committed_at": ""}
            for n in names
            if search.strip("^") in n
        ]

    def commit_file(self, project, **kwargs):
        self.commits.append({"project": project, **kwargs})
        return {"id": "newsha"}

    def create_merge_request(self, project, **kwargs):
        self.merge_requests.append({"project": project, **kwargs})
        return {"iid": 42, "web_url": "https://gitlab.example/mr/42"}

    def get_merge_request(self, project, iid):
        return {"state": "opened"}

    def trigger_pipeline(self, project, ref):
        return {"id": 1, "web_url": ""}


@pytest.fixture
def gitlab(monkeypatch):
    fake = FakeGitLab(MODEL_YAML)
    monkeypatch.setattr(gitlab_client, "GitLabClient", lambda *a, **k: fake)
    # The modules that imported the name at load time.
    import app.api.baselines as baselines_api
    import app.control.baseline_repo as repo
    import app.control.promotion.gitlab as gitlab_target

    monkeypatch.setattr(baselines_api, "GitLabClient", lambda *a, **k: fake)
    monkeypatch.setattr(repo, "GitLabClient", lambda *a, **k: fake)
    monkeypatch.setattr(gitlab_target, "GitLabClient", lambda *a, **k: fake)
    return fake


@pytest_asyncio.fixture
async def client():
    name = f"bind_{next(_counter)}"
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
        session.add(User(id=1, username="sun", password_hash="x", role="admin"))
        session.add(
            Campaign(
                id=1,
                owner_id=1,
                name="kimi night",
                engine="sglang",
                image="registry.example.com/sglang:v0.5.15-cu129",
                model_path="/data/models/kimi",
                served_model_name="kimi",
                search_space={},
                service_port=28200,
                objective={"target_metric": "output_tpm_card_norm"},
            )
        )
        session.add(
            Candidate(
                id=1,
                campaign_id=1,
                kind="search",
                status="exhausted",
                config_hash="h",
                config={
                    "tp": 2,
                    "chunked_prefill_size": 16384,
                    "mem_fraction_static": 0.9,
                    "enable_cache_report": True,
                    "reasoning_parser": "qwen3",
                    "speculative_draft_model_path": "/models/Kimi-DSpark",
                    "attention_backend": "flashinfer",
                },
            )
        )
        session.add(
            Run(
                id=214,
                campaign_id=1,
                candidate_id=1,
                status="succeeded",
                env_snapshot={
                    "card_type": "H100",
                    "image_digest": "registry.example.com/sglang@sha256:abc",
                },
            )
        )
        session.add(
            Result(
                id=1,
                run_id=214,
                source="llmbench",
                objective_value=1875.0,
                feasible=True,
                breaches=[],
                metrics={"output_tpm_card_norm": 1875.0},
            )
        )
        await session.commit()
        token = create_token(await session.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()


async def _baseline(client, **overrides):
    body = {
        "served_model_name": "kimi",
        "engine": "sglang",
        "card_type": "H100",
        "engine_args": {
            "tp": 2,
            "chunked_prefill_size": 16384,
            "mem_fraction_static": 0.85,
            "enable_cache_report": True,
            "reasoning_parser": "qwen3",
            "speculative_draft_model_path": "/models/Kimi-DSpark",
        },
        "image": "registry.example.com/sglang:v0.5.15-cu129",
        "model_path": "/data/models/kimi",
        **overrides,
    }
    response = await client.post("/api/baselines", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _bind(client, baseline_id):
    response = await client.put(
        f"/api/baselines/{baseline_id}/binding",
        json={
            "project": "example-org/model-deploy",
            "branch": "release/modelforge_0.0.2-nvidia_h100-sglang",
            "path": "config/model.yaml",
            "format": {"preset": "helm-release-branch"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- baseline is a whole launch config -------------------------------------------


async def test_a_pasted_docker_run_fills_the_whole_config(client):
    command = (
        "docker run -d --gpus all -e NVIDIA_DISABLE_REQUIRE=1 -v /data/models/kimi:/model "
        "registry.example.com/sglang:v0.5.15-cu129 python -m sglang.launch_server "
        "--model-path /model --served-model-name kimi --port 8050 --tp-size 2 --enable-cache-report"
    )
    preview = (
        await client.post(
            "/api/baselines/parse", json={"served_model_name": "kimi", "command": command}
        )
    ).json()
    assert preview["image"] == "registry.example.com/sglang:v0.5.15-cu129"
    assert preview["model_path"] == "/data/models/kimi"
    assert preview["extra_env"] == {"NVIDIA_DISABLE_REQUIRE": "1"}
    assert preview["engine_args"] == {"tp": "2", "enable_cache_report": True}
    assert preview["cards"] == 2

    created = (
        await client.post(
            "/api/baselines",
            json={
                "served_model_name": "kimi",
                "card_type": "H100",
                "command": command,
            },
        )
    ).json()
    assert created["image"] == "registry.example.com/sglang:v0.5.15-cu129"
    assert created["model_path"] == "/data/models/kimi"
    assert created["engine_args"]["tp"] == "2"
    assert created["binding"] is None

    as_campaign = (await client.get(f"/api/baselines/{created['id']}/as-campaign")).json()
    assert as_campaign["search_space"]["base"] == {"tp": "2", "enable_cache_report": True}
    assert as_campaign["image"] == created["image"]


# -- binding + sync + resolve ------------------------------------------------------


async def test_bind_sync_records_divergences_and_adopts_platform_knobs(client, gitlab):
    baseline = await _baseline(client)
    bound = await _bind(client, baseline["id"])
    assert bound["binding"]["branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert bound["binding"]["commit"] == ""

    # Production moved --mem-fraction-static while we were not looking.
    gitlab.text = MODEL_YAML.replace("0.85", "0.88")
    gitlab.commit = "bbb222"
    synced = (await client.post(f"/api/baselines/{baseline['id']}/binding/sync")).json()
    assert synced["commit"] == "bbb222"
    # The moved knob is adopted; so is env, because the row had none (a blank
    # is filled, not argued over).
    assert [a["key"] for a in synced["adopted"]] == ["mem_fraction_static", "extra_env"]
    assert synced["baseline"]["engine_args"]["mem_fraction_static"] == "0.88"
    assert synced["baseline"]["extra_env"] == {"NVIDIA_DISABLE_REQUIRE": "1"}
    assert (
        synced["baseline"]["source"] == "gitlab:release/modelforge_0.0.2-nvidia_h100-sglang@bbb222"
    )
    # Path-valued knob and the weights path: reported, not overwritten.
    unresolved = {d["key"]: d for d in synced["divergences"] if d["status"] == "unresolved"}
    assert set(unresolved) == {"speculative_draft_model_path", "model_path"}
    assert unresolved["model_path"]["ours"] == "/data/models/kimi"
    assert unresolved["model_path"]["theirs"] == "/mnt/disk0/models/modelforge/release_260817"
    assert synced["baseline"]["model_path"] == "/data/models/kimi"
    assert synced["gpus"] == 2 and synced["env"] == {"NVIDIA_DISABLE_REQUIRE": "1"}

    # Resolve: the draft path is the same weights under another mount.
    resolved = (
        await client.post(
            f"/api/baselines/{baseline['id']}/binding/resolve",
            json={
                "kind": "knob",
                "key": "speculative_draft_model_path",
                "action": "equivalent",
            },
        )
    ).json()
    assert resolved["binding"]["equivalences"]["knobs"]["speculative_draft_model_path"] == [
        ["/models/Kimi-DSpark", "/draft-model"]
    ]
    # Shortcut: the model path is simply not our business.
    quiet = (
        await client.post(
            f"/api/baselines/{baseline['id']}/binding/policy",
            json={"shortcut": "ignore_model_path"},
        )
    ).json()
    assert quiet["binding"]["policy"]["fields"]["model_path"] == "ignore"
    assert quiet["binding"]["unresolved"] == 0
    statuses = {d["key"]: d["status"] for d in quiet["binding"]["divergences"]}
    assert statuses == {"speculative_draft_model_path": "equivalent", "model_path": "ignore"}

    # The next sync is quiet: nothing new to decide.
    again = (await client.post(f"/api/baselines/{baseline['id']}/binding/sync")).json()
    assert again["unresolved"] == 0 and not again["adopted"]

    unknown = await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_all"}
    )
    assert unknown.status_code == 422


async def test_import_creates_a_baseline_from_the_file(client, gitlab):
    imported = (
        await client.post(
            "/api/baselines/import",
            json={
                "served_model_name": "kimi",
                "card_type": "H100",
                "project": "group/deploy",
                "branch": "release/modelforge_0.0.2-nvidia_h100-sglang",
            },
        )
    ).json()
    row = imported["baseline"]
    assert (
        row["engine_args"]["tp"] == "2"
        and row["engine_args"]["speculative_draft_model_path"] == "/draft-model"
    )
    assert row["image"] == "registry.example.com/sglang:v0.5.15-cu129"
    assert row["model_path"] == "/mnt/disk0/models/modelforge/release_260817"
    assert row["binding"]["path"] == "config/model.yaml" and row["binding"]["commit"] == "aaa111"
    assert imported["unresolved"] == 0
    assert (await client.get("/api/baselines/formats")).json()["shortcuts"]


async def test_sync_without_a_binding_is_a_409(client):
    baseline = await _baseline(client)
    assert (await client.post(f"/api/baselines/{baseline['id']}/binding/sync")).status_code == 409


# -- the winner as a merge request ------------------------------------------------


async def test_preview_and_dry_run_promotion_of_a_campaign_winner(client, gitlab):
    baseline = await _baseline(client)
    await _bind(client, baseline["id"])
    await client.post(f"/api/baselines/{baseline['id']}/binding/sync")
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/resolve",
        json={"kind": "knob", "key": "speculative_draft_model_path", "action": "equivalent"},
    )
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_model_path"}
    )

    preview = (await client.post("/api/campaigns/1/promote/preview", json={})).json()
    assert preview["ready"] is True and preview["run_id"] == 214
    assert preview["repo_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert preview["stale"] is False and preview["unresolved"] == 0
    changes = {c["key"]: c for c in preview["plan"]["changes"]}
    assert (
        changes["mem_fraction_static"]["before"] == "0.85"
        and changes["mem_fraction_static"]["after"] == 0.9
    )
    assert changes["attention_backend"]["kind"] == "added"
    # Same draft weights, spelled the repo's way: no change, no report.
    assert "speculative_draft_model_path" not in changes
    assert not [
        r for r in preview["plan"]["reported"] if r["key"] == "speculative_draft_model_path"
    ]
    assert '+  - "--mem-fraction-static=0.9"' in preview["diff"]
    assert '+  - "--attention-backend=flashinfer"' in preview["diff"]
    edited = [
        line
        for line in preview["diff"].splitlines()
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert not [line for line in edited if "/draft-model" in line or "localPath" in line]
    assert preview["dry_run"] is True
    assert "kimi night" in preview["description"] and "requested by sun" in preview["description"]
    assert preview["evidence"]["score"] == 1875.0

    promoted = (await client.post("/api/campaigns/1/promote", json={"target": "gitlab"})).json()
    assert promoted["state"] == "submitted" and promoted["target"] == "gitlab"
    assert promoted["refs"]["dry_run"] is True
    assert promoted["refs"]["target_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert promoted["refs"]["branch"].startswith("autotune/kimi-run214-")
    assert promoted["config"]["origin"]["kind"] == "campaign"
    assert gitlab.commits == [] and gitlab.merge_requests == []  # dry-run wrote nothing

    listed = (await client.get("/api/promotions?campaign_id=1")).json()
    assert [p["id"] for p in listed] == [promoted["id"]]


async def test_armed_promotion_commits_and_opens_the_merge_request(client, gitlab, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "promotion_dry_run", False)
    baseline = await _baseline(client)
    await _bind(client, baseline["id"])
    await client.post(f"/api/baselines/{baseline['id']}/binding/sync")
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_paths"}
    )
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_model_path"}
    )

    promoted = (await client.post("/api/campaigns/1/promote", json={"target": "gitlab"})).json()
    assert promoted["state"] == "submitted", promoted
    assert promoted["refs"]["mr_url"] == "https://gitlab.example/mr/42"
    assert len(gitlab.commits) == 1 and len(gitlab.merge_requests) == 1
    commit = gitlab.commits[0]
    assert commit["start_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert commit["path"] == "config/model.yaml"
    assert '  - "--mem-fraction-static=0.9"\n' in commit["content"]
    assert (
        '  - "--speculative-draft-model-path=/draft-model"\n' in commit["content"]
    )  # ignored: untouched
    assert commit["content"].startswith("# Model developer configuration")  # comments intact
    mr = gitlab.merge_requests[0]
    assert mr["target_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert mr["source_branch"] == commit["branch"]
    assert "```diff" in mr["description"]

    refreshed = (await client.post(f"/api/promotions/{promoted['id']}/refresh")).json()
    assert refreshed["state"] == "submitted" and refreshed["refs"]["mr_state"] == "opened"


async def test_preview_without_a_bound_baseline_says_so(client):
    preview = (await client.post("/api/campaigns/1/promote/preview", json={})).json()
    assert preview["ready"] is False and "no baseline is defined" in preview["reason"]
    baseline = await _baseline(client)
    preview = (await client.post("/api/campaigns/1/promote/preview", json={})).json()
    assert preview["ready"] is False and "not bound" in preview["reason"]
    assert preview["baseline_id"] == baseline["id"]
    # Promoting through gitlab is refused with the same reason, not a 500.
    refused = await client.post("/api/campaigns/1/promote", json={"target": "gitlab"})
    assert refused.status_code == 409 and "not bound" in refused.json()["detail"]


# -- which release branch the winner goes onto -------------------------------------

# The same model, on the same cards, on the NEXT release line: another branch
# of the same repo, with its own file. Nothing here was ever synced.
K25_YAML = MODEL_YAML.replace("--tp-size=2", "--tp-size=4").replace("0.85", "0.80")


async def test_a_campaign_names_the_release_branch_its_winner_goes_onto(client, gitlab):
    baseline = await _baseline(client)
    await _bind(client, baseline["id"])
    await client.post(f"/api/baselines/{baseline['id']}/binding/sync")
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_paths"}
    )
    await client.post(
        f"/api/baselines/{baseline['id']}/binding/policy", json={"shortcut": "ignore_model_path"}
    )
    gitlab.branches["release/kimi-k25-nvidia_h100-sglang"] = (K25_YAML, "ccc333")

    # By default the merge request goes where the baseline is bound.
    preview = (await client.post("/api/campaigns/1/promote/preview", json={})).json()
    assert preview["repo_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert preview["tracked_branch"] == preview["repo_branch"]

    # The campaign names another one.
    campaign = (
        await client.put(
            "/api/campaigns/1/deploy-branch",
            json={"branch": "release/kimi-k25-nvidia_h100-sglang"},
        )
    ).json()
    assert campaign["deploy_branch"] == "release/kimi-k25-nvidia_h100-sglang"

    preview = (await client.post("/api/campaigns/1/promote/preview", json={})).json()
    assert preview["ready"] is True
    assert preview["repo_branch"] == "release/kimi-k25-nvidia_h100-sglang"
    assert preview["tracked_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    assert preview["head_commit"] == "ccc333"
    # Not "stale": that question is about the branch we synced, and this is not
    # it. But the difference is said out loud rather than passed over.
    assert preview["stale"] is False and preview["drift"] == []
    assert any(
        "targets `release/kimi-k25-nvidia_h100-sglang`" in w for w in preview["plan"]["warnings"]
    )
    # The diff is against THAT branch's file: its tp=4 becomes the winner's 2.
    changes = {c["key"]: c for c in preview["plan"]["changes"]}
    assert changes["tp"]["before"] == "4" and changes["tp"]["after"] == 2
    assert '-  - "--tp-size=4"' in preview["diff"]
    assert '+  - "--tp-size=2"' in preview["diff"]

    # A branch named on the request wins, and sticks to the campaign.
    promoted = (
        await client.post(
            "/api/campaigns/1/promote",
            json={"target": "gitlab", "branch": "release/modelforge_0.0.2-nvidia_h100-sglang"},
        )
    ).json()
    assert promoted["refs"]["target_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"
    campaign = (await client.get("/api/campaigns/1")).json()
    assert campaign["deploy_branch"] == "release/modelforge_0.0.2-nvidia_h100-sglang"


async def test_the_branch_list_comes_from_the_repo(client, gitlab):
    response = await client.get("/api/baselines/branches?project=group/deploy&search=^release/")
    listed = response.json()
    assert [b["name"] for b in listed["branches"]] == [
        "release/modelforge_0.0.2-nvidia_h100-sglang"
    ]
    assert listed["error"] == ""


async def test_the_branch_list_is_empty_rather_than_an_error_without_gitlab(client):
    listed = (await client.get("/api/baselines/branches?project=group/deploy")).json()
    assert listed["branches"] == [] and "not configured" in listed["error"]


def test_the_stored_copy_stands_only_for_the_branch_it_came_from():
    """Offline, a preview falls back to the file saved at the last sync. That
    file is one branch's; answering with it for another would diff a config
    against the wrong production, so it is refused instead."""
    from app.control.baseline_repo import fetch_document
    from app.control.gitlab_client import GitLabUnavailable
    from app.db.models import DeployBinding

    binding = DeployBinding(
        project="group/deploy",
        branch="release/modelforge_0.0.2-nvidia_h100-sglang",
        path="config/model.yaml",
        document=MODEL_YAML,
        commit="aaa111",
    )

    class Unconfigured:
        configured = False

    tracked = fetch_document(binding, Unconfigured())
    assert tracked.offline and tracked.text == MODEL_YAML

    with pytest.raises(GitLabUnavailable) as exc:
        fetch_document(binding, Unconfigured(), ref="release/kimi-k25-nvidia_h100-sglang")
    assert "only has a copy of" in str(exc.value)
