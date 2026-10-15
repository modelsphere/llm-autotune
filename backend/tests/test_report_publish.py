"""A report published outside the platform carries its measurements, not the
site it was measured on."""

import json

from app.agent.publish import publishable


def _run(run_id: int, label: str) -> dict:
    return {
        "run_id": run_id,
        "label": label,
        "launch": {
            "run_id": run_id,
            "config": {
                "engine": "sglang",
                "image": "lmsysorg/sglang:v0.5.15",
                "model_path": "/mnt/disk0/models/org/Model-X",
                "served_model_name": "model-x",
                "extra_env": {"SGLANG_X": "1", "NCCL_SOCKET_IFNAME": "bond0"},
                "extra_volumes": {"/mnt/disk0/models/Model-X-EAGLE": "/draft"},
            },
            "rendered": {
                "docker_command": "docker run --name autotune-run-7 "
                "-v /mnt/disk0/models/org/Model-X:/model ...",
                "serve_command": "python -m sglang.launch_server --model-path "
                "/mnt/disk0/models/org/Model-X --speculative-draft-model-path "
                "/mnt/disk0/models/Model-X-EAGLE --tp-size 2",
                "engine_flags": {"tp_size": "2"},
            },
            "origin": {
                "source": "campaign",
                "campaign_id": 9,
                "campaign_name": "lab tuning",
                "candidate_id": 4,
            },
        },
        "environment": {
            "run_id": run_id,
            "machine": {
                "name": "gpu-box-17 (H100)",
                "driver": "ssh_docker",
                "gpu_type": "H100",
                "gpu_count": 8,
            },
            "gpu_indices": [4, 5],
            "card_type": "H100",
        },
        "results": {
            "run_id": run_id,
            "links": {"raw": f"/api/agent/v1/runs/{run_id}/results/raw"},
            "failure": {"error": "ssh gpu-box-17 timed out", "log": "secret log"},
            "scenarios": [{"key": "s", "best_level": {"total_tps_per_machine": 100.0}}],
        },
    }


def test_the_site_is_gone_and_the_measurements_stay():
    doc = {
        "comparable": True,
        "group": {"kind": "track", "slug": "lab-track"},
        "links": {"self": "/api/agent/v1/comparison?baseline=101&attempts=102"},
        "benchmark": {
            "llmbench": {
                "slug": "bench",
                "benchmark_id": 3,
                "url": "http://bench.example/b/3",
                "submission_id": "abc",
                "submission_url": "http://bench.example/s/abc",
            },
            "platform": {"ranking_metric": "tps", "extensions": {"hub": {"track": "t"}}},
        },
        "baseline": _run(101, "lab tuning · run 101"),
        "attempts": [
            {
                **_run(102, "lab tuning · run 102"),
                "position": 1,
                "diff_vs_baseline": {
                    "extra_env": {"added": {"NCCL_SOCKET_IFNAME": "x", "SGLANG_Y": "2"}}
                },
            }
        ],
        "best": {"overall": {"run_id": 102, "label": "lab tuning · run 102"}},
        "series": [
            {"lines": [{"run_id": 101, "label": "lab tuning · run 101", "points": [[1, 2]]}]}
        ],
    }
    out = publishable(doc)
    text = json.dumps(out)
    for leak in (
        "gpu-box-17",
        "/mnt/disk0",
        "lab tuning",
        "run 101",
        "/api/agent",
        "lab-track",
        "bench.example",
        "bond0",
        "secret log",
        "autotune-run-7",
    ):
        assert leak not in text, leak
    assert out["baseline"]["run_id"] == 1 and out["attempts"][0]["run_id"] == 2
    assert out["best"]["overall"] == {"run_id": 2, "label": "Optimized"}
    assert out["series"][0]["lines"][0]["label"] == "Baseline"
    cfg = out["baseline"]["launch"]["config"]
    assert cfg["model_path"] == "/model" and cfg["extra_volumes"] == {}
    assert cfg["extra_env"] == {"SGLANG_X": "1"}, "the recipe's env stays, the site's goes"
    serve = out["baseline"]["launch"]["rendered"]["serve_command"]
    assert "--model-path /model --speculative-draft-model-path /draft" in serve
    assert out["attempts"][0]["diff_vs_baseline"]["extra_env"]["added"] == {"SGLANG_Y": "2"}
    # What the report draws is untouched.
    assert out["baseline"]["results"]["scenarios"] == doc["baseline"]["results"]["scenarios"]
    assert out["baseline"]["environment"]["machine"]["gpu_type"] == "H100"
    assert out["benchmark"]["platform"]["ranking_metric"] == "tps"
    assert doc["baseline"]["label"] == "lab tuning · run 101", "the saved report is not changed"
