"""A recipe whose engine command is a script (`commandOverride`) and whose
cards are named under `resources`: what it claims, and the flags it passes,
read for showing, never written."""

from app.control.launch_config import LaunchConfig
from app.control.recipe.command import command_flags, recipe_flags
from app.control.recipe.model import Recipe
from app.control.recipe.shape import accelerator_request, shape

SCRIPT = """set -euo pipefail
RANK="${LWS_WORKER_INDEX:-0}"
case "$RANK" in 0|1) ;; *) echo "Invalid node rank: $RANK" >&2; exit 1 ;; esac
if [ -f /usr/lib/libjemalloc.so.2 ]; then
  export LD_PRELOAD=/usr/lib/libjemalloc.so.2
fi
for _ in $(seq 1 60); do
  if LEADER_IP=$(getent hosts "$LWS_LEADER_ADDRESS"); then break; fi
  sleep 5
done
set -- /models \\
  --port 8000 \\
  --served-model-name "${SERVED_MODEL_NAME}" \\
  --data-parallel-size 2 \\
  --tensor-parallel-size 8 \\
  --enable-expert-parallel \\
  --speculative-config '{"method":"dspark","num_speculative_tokens":5}'
if [ "$RANK" = 1 ]; then
  set -- "$@" --headless --data-parallel-start-rank 1
fi
exec vllm serve "$@" --data-parallel-address "$LEADER_IP"
"""


def _ascend(**values) -> Recipe:
    return Recipe(
        engine="vllm",
        values={
            "image": {"repository": "quay.io/ascend/vllm-ascend",
                      "tag": "pinned@sha256:" + "7" * 64},
            "model": {"gpus": "0", "mountPath": "/models"},
            "lws": {"enabled": True, "size": 2},
            "resources": {
                "requests": {"volcano.sh/Ascend910B4-1": "8", "cpu": "32", "memory": "768Gi"},
                "limits": {"volcano.sh/Ascend910B4-1": "8", "cpu": "32", "memory": "768Gi"},
            },
            "commandOverride": ["bash", "-c", SCRIPT],
            **values,
        },
    )


def test_cards_named_under_resources_are_what_each_pod_claims():
    s = shape(_ascend())
    assert (s.max_pod_gpus, s.total_gpus, s.pods) == (8, 16, 2)


def test_model_gpus_wins_and_rdma_or_cpu_are_not_cards():
    assert shape(_ascend(model={"gpus": "4"})).max_pod_gpus == 4
    assert accelerator_request(
        {"resources": {"limits": {"rdma/hca_shared": "1", "cpu": "8", "hugepages-2Mi": "1Gi"}}}
    ) == 0
    assert shape(Recipe(values={"model": {"gpus": "0"}})).total_gpus == 0


def test_the_scripts_flags_are_read_and_a_ranks_own_kept_apart():
    found = command_flags(_ascend())
    assert found.flags["tp"] == 8 and found.flags["dp"] == 2
    assert found.flags["enable_expert_parallel"] is True
    assert found.flags["speculative_config"] == '{"method":"dspark","num_speculative_tokens":5}'
    # Filled in when the pod starts: kept as written.
    assert found.flags["served_model_name"] == "${SERVED_MODEL_NAME}"
    assert found.flags["data_parallel_address"] == "$LEADER_IP"
    assert "" not in found.flags, "`set --` and the model path are not flags"
    # Only the worker pod gets these.
    assert found.conditional == {"headless": True, "data_parallel_start_rank": 1}


def test_a_recipe_without_a_script_has_no_command_flags():
    assert command_flags(Recipe(values={"extraArgs": ["--tp-size=2"]})).flags == {}
    one_line = command_flags(Recipe(engine="sglang", values={
        "commandOverride": ["sglang", "serve", "--model-path", "/model", "--tp-size", "4"]}))
    assert one_line.flags["tp"] == 4


def test_a_branch_on_one_line_is_still_a_branch():
    found = command_flags(Recipe(values={"commandOverride": ["bash", "-c", (
        'case "$R" in 0|1) ;; *) exit 1 ;; esac\n'
        "set -- /m --tensor-parallel-size 8\n"
        'if [ "$R" = 1 ]; then set -- "$@" --headless; fi\n'
        'exec vllm serve "$@"\n')]}))
    assert found.flags == {"tp": 8}
    assert found.conditional == {"headless": True}


def test_a_scripted_recipe_reads_as_the_flags_its_script_passes():
    recipe = _ascend()
    assert recipe_flags(recipe) == command_flags(recipe).flags
    cfg = LaunchConfig.from_recipe(recipe, model_path="/data/m")
    assert cfg.engine_args["tp"] == 8 and "headless" not in cfg.engine_args
    assert recipe_flags(Recipe(values={"extraArgs": ["--tp-size=2"]})) == {"tp": 2}
