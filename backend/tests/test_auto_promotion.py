"""A finished campaign proposes its own winner.

`auto_promote` is the option that turns "the platform found a better config"
into a merge request without anyone present. What these tests hold it to: it
promotes the SAME run the leaderboard puts on top, it goes to the release
branch the campaign names, it happens once, it never proposes a run that
crosses a redline, and a campaign that never produced a winner is not retried
forever.

GitLab is faked at the client boundary, so the real adapter, policy, draft and
target code runs against a real file shape with no network.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control import gitlab_client
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.db.base import Base
from app.db.models import (
    Baseline,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    DeployBinding,
    Event,
    Machine,
    MachineState,
    Promotion,
    Result,
    Run,
    RunStatus,
    User,
)
from tests.fakes import NullDriver
from tests.test_deploy_binding_api import MODEL_YAML, FakeGitLab

TRACKED = "release/modelforge_0.0.2-nvidia_h100-sglang"
OTHER = "release/kimi-k25-nvidia_h100-sglang"


def _platform(*, auto_promote=True, deploy_branch="", bound=True):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(
                id=1, name="h800", host="10.0.0.1", gpu_count=8, gpu_type="H100",
                state=MachineState.AVAILABLE.value,
            )
        )
        session.add(
            Campaign(
                id=1, owner_id=1, name="kimi mtp", engine="sglang",
                image="registry.example.com/sglang:v0.5.15-cu129",
                model_path="/mnt/disk0/models/modelforge/release_260817",
                served_model_name="kimi",
                search_space={"grid": {"mem_fraction_static": [0.85, 0.9]}},
                objective={"target_metric": "perf.output_tpm_card_norm"},
                benchmark_slug="autotune-replay-short-qwen36",
                status=CampaignStatus.DONE.value,
                auto_promote=auto_promote,
                deploy_branch=deploy_branch,
            )
        )
        baseline = Baseline(
            id=1, served_model_name="kimi", engine="sglang", card_type="H100",
            engine_args={"tp": "2", "chunked_prefill_size": "16384",
                         "mem_fraction_static": "0.85", "enable_cache_report": True},
            image="registry.example.com/sglang:v0.5.15-cu129",
            model_path="/mnt/disk0/models/modelforge/release_260817",
            source="manual",
        )
        if bound:
            baseline.binding = DeployBinding(
                project="group/deploy", branch=TRACKED, path="config/model.yaml",
                format={"preset": "helm-release-branch"},
                policy={"fields": {"model_path": "ignore", "image": "ignore"},
                        "path_knobs": "ignore"},
                commit="aaa111", document=MODEL_YAML,
            )
        session.add(baseline)
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _run(session, run_id, candidate_id, config, value, *, feasible=True, breaches=()):
    session.add(
        Candidate(
            id=candidate_id, campaign_id=1, config=config,
            config_hash=CandidateConfig(engine_args=config).hash,
            kind=CandidateKind.SEARCH.value, status=CandidateStatus.EXHAUSTED.value,
        )
    )
    session.add(
        Run(
            id=run_id, campaign_id=1, candidate_id=candidate_id, machine_id=1,
            status=RunStatus.SUCCEEDED.value,
            env_snapshot={"card_type": "H100"},
        )
    )
    session.add(
        Result(
            run_id=run_id, source="llmbench", passed=True,
            metrics={"perf.output_tpm_card_norm": value},
            objective_value=value, feasible=feasible, breaches=list(breaches),
        )
    )


def _gitlab(monkeypatch, text=MODEL_YAML):
    fake = FakeGitLab(text)
    import app.control.baseline_repo as repo
    import app.control.promotion.gitlab as target

    monkeypatch.setattr(gitlab_client, "GitLabClient", lambda *a, **k: fake)
    monkeypatch.setattr(repo, "GitLabClient", lambda *a, **k: fake)
    monkeypatch.setattr(target, "GitLabClient", lambda *a, **k: fake)
    return fake


def _arm(monkeypatch, supervisor, *, dry_run=True):
    monkeypatch.setattr(supervisor.settings, "promotion_target", "gitlab")
    monkeypatch.setattr(supervisor.settings, "promotion_dry_run", dry_run)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "promotion_dry_run", dry_run)


def test_a_finished_campaign_proposes_the_run_the_board_puts_on_top(monkeypatch):
    supervisor, factory = _platform()
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor, dry_run=False)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.85}, 1500.0)
        _run(session, 11, 2, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)  # the winner
        session.commit()

    supervisor.tick()

    with factory() as session:
        promotions = session.scalars(select(Promotion)).all()
        assert len(promotions) == 1
        promotion = promotions[0]
        assert promotion.run_id == 11  # the top of the board, not the first run
        assert promotion.state == "submitted"
        assert promotion.target == "gitlab"
        assert promotion.refs["target_branch"] == TRACKED
    # It really opened one, with the winner's value in the file.
    assert len(gitlab.commits) == 1 and len(gitlab.merge_requests) == 1
    assert '  - "--mem-fraction-static=0.9"\n' in gitlab.commits[0]["content"]
    assert gitlab.merge_requests[0]["target_branch"] == TRACKED
    assert "Opened automatically" in gitlab.merge_requests[0]["description"]


def test_it_goes_to_the_branch_the_campaign_names(monkeypatch):
    supervisor, factory = _platform(deploy_branch=OTHER)
    gitlab = _gitlab(monkeypatch)
    gitlab.branches[OTHER] = (MODEL_YAML.replace("--tp-size=2", "--tp-size=4"), "ccc333")
    _arm(monkeypatch, supervisor, dry_run=False)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()

    assert gitlab.merge_requests[0]["target_branch"] == OTHER
    assert gitlab.commits[0]["start_branch"] == OTHER
    # Diffed against THAT branch's file: its tp=4 becomes the winner's 2.
    assert '  - "--tp-size=2"\n' in gitlab.commits[0]["content"]


def test_dry_run_records_the_draft_and_writes_nothing(monkeypatch):
    supervisor, factory = _platform()
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor, dry_run=True)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        promotion = session.scalars(select(Promotion)).one()
        assert promotion.state == "submitted" and promotion.refs["dry_run"] is True
        assert promotion.refs["target_branch"] == TRACKED
    assert gitlab.commits == [] and gitlab.merge_requests == []


def test_it_happens_once(monkeypatch):
    supervisor, factory = _platform()
    _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()
    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        assert len(session.scalars(select(Promotion)).all()) == 1


def test_a_winner_that_crosses_a_redline_is_not_proposed(monkeypatch):
    supervisor, factory = _platform()
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0,
             feasible=False, breaches=["ttft_p95 > 2000"])
        session.commit()

    supervisor.tick()

    with factory() as session:
        assert session.scalars(select(Promotion)).all() == []
        kinds = [e.kind for e in session.scalars(select(Event)).all()]
        assert "auto_promotion_skipped" in kinds
    assert gitlab.commits == []


def test_a_campaign_with_no_successful_run_is_skipped_once(monkeypatch):
    supervisor, factory = _platform()
    _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)

    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        assert session.scalars(select(Promotion)).all() == []
        skips = [e for e in session.scalars(select(Event)).all()
                 if e.kind == "auto_promotion_skipped"]
        assert len(skips) == 1
        assert "no successful" in skips[0].payload["reason"]


def test_an_unbound_baseline_leaves_a_failed_row_rather_than_silence(monkeypatch):
    supervisor, factory = _platform(bound=False)
    _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        promotion = session.scalars(select(Promotion)).one()
        assert promotion.state == "failed"
        assert "not bound" in promotion.error


def test_off_by_default(monkeypatch):
    supervisor, factory = _platform(auto_promote=False)
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)
    with factory() as session:
        _run(session, 10, 1, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        assert session.scalars(select(Promotion)).all() == []
    assert gitlab.commits == []


def test_a_winner_that_is_already_production_is_a_no_op_not_a_failure(monkeypatch):
    """The whole point of tuning is that sometimes the answer is "what you are
    already running". That must not show up as a red failed promotion on a
    campaign that did its job."""
    supervisor, factory = _platform()
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor, dry_run=False)
    with factory() as session:
        # The file's own configuration, knob for knob.
        _run(session, 10, 1, {"tp": 2, "chunked_prefill_size": 16384,
                              "mem_fraction_static": 0.85, "enable_cache_report": True}, 1900.0)
        session.commit()

    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        assert session.scalars(select(Promotion)).all() == []
        skips = [e for e in session.scalars(select(Event)).all()
                 if e.kind == "auto_promotion_skipped"]
        assert len(skips) == 1  # and not retried on the second tick
        assert "already runs this configuration" in skips[0].payload["reason"]
    assert gitlab.commits == [] and gitlab.merge_requests == []


def test_a_campaign_that_finds_a_winner_late_still_proposes_it(monkeypatch):
    """DONE is not final. A campaign whose every launch was refused reaches
    DONE with nothing to promote; retrying the failed runs revives it and it
    goes on to find a winner. A pass that disarmed the flag on the way past
    would never propose that winner — which is exactly what happened on
    campaign 149."""
    supervisor, factory = _platform()
    gitlab = _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor, dry_run=False)

    supervisor.tick()  # nothing succeeded yet
    supervisor.tick()  # …and it does not say so twice
    with factory() as session:
        skips = [e for e in session.scalars(select(Event)).all()
                 if e.kind == "auto_promotion_skipped"]
        assert len(skips) == 1
        assert session.get(Campaign, 1).auto_promote is True

    with factory() as session:  # the retry lands
        _run(session, 20, 5, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        promotion = session.scalars(select(Promotion)).one()
        assert promotion.run_id == 20 and promotion.state == "submitted"
    assert len(gitlab.merge_requests) == 1


def test_a_different_reason_is_news(monkeypatch):
    supervisor, factory = _platform()
    _gitlab(monkeypatch)
    _arm(monkeypatch, supervisor)

    supervisor.tick()  # no successful run
    with factory() as session:  # now one, but it crosses a redline
        _run(session, 21, 6, {"tp": 2, "mem_fraction_static": 0.9}, 1900.0,
             feasible=False, breaches=["ttft_p95 > 2000"])
        session.commit()
    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        reasons = [e.payload["reason"] for e in session.scalars(select(Event)).all()
                   if e.kind == "auto_promotion_skipped"]
        assert reasons == ["no successful, benchmarked run to promote",
                           "the best run crosses a redline"]
