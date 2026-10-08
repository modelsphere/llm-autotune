"""The replay module's name is the deployment's, not a constant.

LLMBench names its traffic replay module "replay". A deployment whose
LLMBench names it otherwise sets AUTOTUNE_LLMBENCH_REPLAY_MODULE, and every
place a replay metric is looked up follows: the catalog, the default
second-stage objective, and the keys dataset pinning reads what a run
replayed from. The name is read once, at import, so each case runs in a
fresh interpreter.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

PROBE = """
import json
from app.datasets import pinning
from app.metrics_catalog import DEFAULT_VERIFY_TARGET_METRIC, METRICS
print(json.dumps({
    "verify": DEFAULT_VERIFY_TARGET_METRIC,
    "pin": [pinning.METRIC_BUILD_ID, pinning.METRIC_SHA],
    "catalog": sorted({m["key"].split(".")[0] for m in METRICS}),
}))
"""


def _names(**env) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=BACKEND,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_by_default_it_is_llmbenchs_own_name():
    names = _names(AUTOTUNE_LLMBENCH_REPLAY_MODULE="replay")
    assert names["verify"] == "replay.score_card_norm"
    assert names["pin"] == ["replay.dataset_id", "replay.dataset_sha256"]
    assert "replay" in names["catalog"]


def test_a_deployment_can_name_it_otherwise():
    names = _names(AUTOTUNE_LLMBENCH_REPLAY_MODULE="replay_v2")
    assert names["verify"] == "replay_v2.score_card_norm"
    assert names["pin"] == ["replay_v2.dataset_id", "replay_v2.dataset_sha256"]
    assert "replay_v2" in names["catalog"] and "replay" not in names["catalog"]
