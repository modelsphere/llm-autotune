# Mock engine

A stdlib-only fake sglang/vLLM server, for running the whole tuning loop without
GPUs. It answers to both `python3 -m sglang.launch_server ...` and
`vllm serve ...`, so the platform launches it exactly as it would a real engine,
on Kubernetes or over ssh.

No image is published: it is a few kilobytes of Python on `python:3.12-slim`,
built where it is used. [deploy/demo.sh](../deploy/demo.sh) builds
it as `llm-autotune-mock-engine:<version>` and loads it into the cluster, or
pushes it to the registry named with `--registry`. By hand:
`docker build -t llm-autotune-mock-engine:<version> mock-engine/`, then
`kind load docker-image …` (or push it where the nodes pull from).

A run's model path must be a directory that exists where the engine starts and
is not empty: the platform refuses to start an engine on an empty weights
directory, which would otherwise surface as a confusing crash deep inside a
real engine. The mock ignores what is in it, so a placeholder is enough.

**On Kubernetes** the chart provides one: with `mockModel.enabled` (on in
`values-demo.yaml`, which [deploy/demo.sh](../deploy/demo.sh)
installs with) a DaemonSet writes `/var/lib/llm-autotune/mock-model/config.json`
on every node, using the mock image (`mockModel.image`), so every node has the
image before the first run. Use `/var/lib/llm-autotune/mock-model` as the
campaign's model path.

**On an ssh machine**, build the image there (or
`docker save <image> | ssh <host> docker load`), make a placeholder
(`mkdir -p /tmp/mock-model && echo {} > /tmp/mock-model/config.json`) and use
it as the model path. On a box without GPUs, set `AUTOTUNE_DOCKER_GPU_MODE=none`
for the worker so the driver omits `--gpus/--runtime=nvidia`.

Behavior knobs go in the search space like normal engine args, e.g.:

```json
{
  "base": {"mock_max_output_tokens": 64},
  "grid": {"mock_token_ms": [2, 20], "mock_latency_ms": [20, 200]}
}
```

Crash simulation: `{"mock_fail": "oom"}` (startup OOM),
`{"mock_fail_after_seconds": 30}` (OOM under load).

Timing knobs, so a benchmark that streams gets a shape to measure. They default
to fast, so a run finishes in a minute or two:
`mock_startup_seconds` (before the port opens, default 1), `mock_latency_ms`
(time to the first token, default 20), `mock_token_ms` (gap between streamed
tokens, default 1) and `mock_max_output_tokens` (cap on a reply, default 256; a
request's `max_tokens` decides the length below that). `"stream": true` answers
server-sent events one token per chunk, with `usage` on request — the wire
format guidellm and LLMBench's throughput sweeps time — so a real LLMBench can
benchmark the mock and report numbers. They describe the mock's timers, not a
model.

On a laptop or a small CPU-only cluster, the default screen benchmark
(`autotune-screen-v1`: 2048-token prompts, up to 64 concurrent streams)
measures how fast the benchmark client can prepare requests rather than the
mock, and two settings rank by noise. Screen mock campaigns with
`autotune-quickstart-v1` instead (short requests, concurrency 1 and 4), as the
demo does; `deploy/demo.sh` creates it on LLMBench.
