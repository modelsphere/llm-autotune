"""(1.9) A policy may run a command in a serving launch's engine.

The launch is tainted from then on: the campaign's own benchmark is refused on
it, since what it serves may no longer be what its config says. A probe still
runs, and a relaunch of the config is clean.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

from sqlalchemy import select

from app.api import policy_sessions
from app.db.models import Event, Run
from tests.test_external_policy import _make_external, _open
from tests.test_policy_probes import llmbench  # noqa: F401  (fixture)
from tests.test_policy_sessions import _serving_launch, night  # noqa: F401  (fixture)


async def test_a_command_runs_in_the_engine_and_taints_the_launch(night, monkeypatch):
    supervisor, driver, http = night
    monkeypatch.setattr(policy_sessions, "_engine_driver", lambda machine: driver)
    await _make_external(http)
    policy = await _open(supervisor, http)
    run_id = await _serving_launch(supervisor, policy)

    ran = await policy.post(f"/launches/{run_id}/exec", json={"command": "nvidia-smi -L"})
    assert ran.status_code == 200, ran.text
    assert ran.json() == {"exit_code": 0, "output": "ran: nvidia-smi -L\n", "tainted": True}
    assert driver.execs == [(f"autotune-run-{run_id}", "nvidia-smi -L")]
    assert (await policy.get(f"/launches/{run_id}")).json()["tainted"] is True
    async with http.db() as session:
        assert (await session.get(Run, run_id)).tainted is True
        kinds = [e.kind for e in (await session.execute(select(Event))).scalars()]
        assert "policy_exec_requested" in kinds and "policy_exec" in kinds


async def test_the_campaigns_benchmark_is_refused_on_a_tainted_launch(night, llmbench, monkeypatch):
    supervisor, driver, http = night
    monkeypatch.setattr(policy_sessions, "_engine_driver", lambda machine: driver)
    await _make_external(http)
    policy = await _open(supervisor, http)
    run_id = await _serving_launch(supervisor, policy)
    await policy.post(f"/launches/{run_id}/exec", json={"command": "true"})

    screen = await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    assert screen.status_code == 409
    assert "tainted" in screen.text
    probe = await policy.post(
        f"/launches/{run_id}/benchmarks",
        json={"benchmark": {"kind": "sweep", "concurrencies": [4]}},
    )
    assert probe.status_code == 201, probe.text  # a probe still runs


async def test_only_a_serving_launch_takes_a_command(night):
    supervisor, _driver, http = night
    await _make_external(http)
    policy = await _open(supervisor, http)
    queued = await policy.post("/launches", json={"engine_args": {"tp_size": 2}})
    refused = await policy.post(f"/launches/{queued.json()['id']}/exec", json={"command": "true"})
    assert refused.status_code == 409
    assert (await policy.get(f"/launches/{queued.json()['id']}")).json()["tainted"] is False
