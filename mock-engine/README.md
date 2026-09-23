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

Timing knobs, so a benchmark that streams gets a shape to measure:
`mock_latency_ms` (time to the first token, default 200), `mock_token_ms`
(gap between streamed tokens, default 5) and `mock_max_output_tokens` (cap on
a reply, default 256; a request's `max_tokens` decides the length below that).
`"stream": true` answers server-sent events one token per chunk, with `usage`
on request — the wire format guidellm and LLMBench's throughput sweeps time —
so a real LLMBench can benchmark the mock and report numbers. They describe
the mock's timers, not a model.

On a CPU-only dev box set `AUTOTUNE_DOCKER_GPU_MODE=none` for the worker so the
driver omits `--gpus/--runtime=nvidia`.
