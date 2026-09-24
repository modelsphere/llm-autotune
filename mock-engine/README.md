# Mock engine

A stdlib-only fake sglang/vLLM server, for running the whole tuning loop without
GPUs. It answers to both `python3 -m sglang.launch_server ...` and
`vllm serve ...`, so the platform launches it exactly as it would a real engine,
on Kubernetes or over ssh.

No image is published; build it where the platform's runs can use it.

**On a kind cluster** (the README quickstart):

```bash
docker build -t llm-autotune-mock-engine:0.1.0 mock-engine/
kind load docker-image llm-autotune-mock-engine:0.1.0
```

Engine pods use `imagePullPolicy: IfNotPresent`, so the loaded image is used as
is. On any other cluster, push it to a registry the nodes pull from.

**On an ssh machine**, build it on the machine, or copy it over with
`docker save llm-autotune-mock-engine:0.1.0 | ssh <host> docker load`. On a box
without GPUs, set `AUTOTUNE_DOCKER_GPU_MODE=none` for the worker so the driver
omits `--gpus/--runtime=nvidia`.

Then create a campaign with that image. Its `model_path` must be a directory
that exists on the node or machine and is not empty: the platform refuses to
start an engine on an empty weights directory, which would otherwise surface
as a confusing crash deep inside a real engine. The mock ignores what is in it,
so one placeholder file is enough (on kind:
`docker exec kind-control-plane sh -c 'mkdir -p /models/mock && echo {} > /models/mock/config.json'`).

Behavior knobs go in the search space like normal engine args, e.g.:

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
