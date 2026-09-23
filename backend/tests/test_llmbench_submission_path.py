"""The body a real benchmark submission carries, end to end.

Every other test that benchmarks a run swaps the evaluator for a stub, which
is how a supervisor that built its context and then forgot to return it could
pass the whole suite while failing every real submission. This one keeps the
real pieces — the supervisor's context, the LLMBench evaluator, the HTTP client
— and replaces only the network, then reads what was sent.
"""

import json

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Machine,
    MachineState,
    Run,
    RunStatus,
    User,
)
from app.evaluation.llmbench import LLMBenchClient, LLMBenchEvaluator
from tests.fakes import NullDriver


def _stack():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(Machine(id=1, name="gpu-a", host="198.51.100.10", gpu_count=8,
                            gpu_type="H100", state=MachineState.AVAILABLE.value,
                            baseline_status=BaselineStatus.CLEARED.value))
        session.add(Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                             model_path="/m", served_model_name="m",
                             search_space={"grid": {"tp": [2]}},
                             benchmark_slug="autotune-screen-v1",
                             status=CampaignStatus.ACTIVE.value, run_baseline_canary=False))
        session.add(Candidate(id=1, campaign_id=1, config={"tp": 2}, config_hash="h",
                              status=CandidateStatus.VALID.value))
        session.add(Run(id=7, campaign_id=1, candidate_id=1, machine_id=1,
                        status=RunStatus.BENCHING.value,
                        endpoint_url="http://198.51.100.10:28200"))
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def test_a_submission_carries_the_benchmark_the_hardware_and_the_config():
    sent: list[httpx.Request] = []

    def llmbench(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/submissions/preflight":
            return httpx.Response(200, json={"ok": True, "checks": []})
        return httpx.Response(202, json={"id": 41, "status": "queued"})

    supervisor, factory = _stack()
    evaluator = LLMBenchEvaluator(client=LLMBenchClient(
        base_url="http://llmbench.test", api_key="llmb_service",
        transport=httpx.MockTransport(llmbench)))

    with factory() as session:
        run = session.get(Run, 7)
        context = supervisor._bench_context(run)
        assert context is not None, "the supervisor built a context and did not return it"
        ref = evaluator.start(run.endpoint_url, run.campaign.served_model_name, context)

    assert ref == "41"
    submit = sent[-1]
    assert submit.url.path == "/submissions/benchmarks/autotune-screen-v1/submit"
    assert submit.headers["authorization"] == "Bearer llmb_service"
    body = json.loads(submit.content)
    assert body["endpoint_url"] == "http://198.51.100.10:28200"
    assert body["model"] == "m"
    # The cards the CONFIG occupies (tp=2), not the size of the box (8): that
    # is what makes LLMBench's card-normalized throughput comparable.
    assert (body["cards_per_machine"], body["machine_count"], body["card_type"]) == (2, 1, "H100")
    assert "tp=2" in body["description_summary"]
