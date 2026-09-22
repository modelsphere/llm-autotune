# Mock engine image

A stdlib-only fake sglang/vllm server for exercising the autotune flow without
GPUs. The unmodified ssh_docker driver launches it: it answers to both
`python3 -m sglang.launch_server ...` and `vllm serve ...`.

Build on the dev box (or any machine the worker can ssh to):

```bash
docker build -t autotune-mock-engine:latest mock-engine/
```

Then create a campaign with `image: autotune-mock-engine:latest` and any
`model_path` that exists on the machine (content is ignored). Behavior knobs go
in the search space like normal engine args, e.g.:

```json
{
  "base": {"mock_startup_seconds": 5},
  "grid": {"tp_size": [1, 2], "mock_latency_ms": [100, 400]}
}
```

Crash simulation: `{"mock_fail": "oom"}` (startup OOM),
`{"mock_fail_after_seconds": 30}` (OOM under load).

On a CPU-only dev box set `AUTOTUNE_DOCKER_GPU_MODE=none` for the worker so the
driver omits `--gpus/--runtime=nvidia`.
