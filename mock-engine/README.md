# Mock engine

A stdlib-only fake sglang/vLLM server, for running the whole tuning loop without
GPUs. It answers to `python3 -m sglang.launch_server ...`, `sglang serve ...`
(what the engine charts run) and `vllm serve ...`, so the platform launches it exactly as it would a real engine,
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

On a workstation or a small CPU-only cluster, the default screen benchmark
(`autotune-screen-v1`: 2048-token prompts, up to 64 concurrent streams)
measures how fast the benchmark client can prepare requests rather than the
mock, and two settings rank by noise. Screen mock campaigns with
`autotune-quickstart-v1` instead (short requests, concurrency 1 and 4), as the
demo does; `deploy/demo.sh` creates it on LLMBench.

## A landscape to search

By default the mock ignores real engine flags, so every config measures the
same. `--mock-landscape sglang` (put `mock_landscape: sglang` in the space's
`base`) makes real sglang flags move the numbers instead — roughly, and on
purpose simply ([landscape.py](landscape.py)), so a search policy can be run end
to end without a GPU and still have something to find:

- `mem_fraction_static` sets the KV-cache capacity; when it is full, requests
  wait, so throughput at high concurrency depends on it. Above 0.93 the engine
  fails at startup with a CUDA out-of-memory error, as do a `cuda_graph_max_bs`
  above 384 or a `chunked_prefill_size` above 32768 with more than 0.90.
- `max_running_requests` caps the batch; `cuda_graph_max_bs` below the batch
  size makes every decode step slower; `attention_backend`,
  `enable_torch_compile`, `num_continuous_decode_steps`, `schedule_policy` and
  `enable_mixed_chunk` shift it a little; `chunked_prefill_size` trades prefill
  speed against decode interference.
- Speculative decoding (`speculative_algorithm NEXTN`) speeds small batches and
  slows large ones; any other algorithm without `speculative_draft_model_path`
  fails at startup.
- Each engine process has about 1.5% noise, and it logs sglang-style
  `Decode batch.` lines and answers `/get_server_info` with its resolved
  arguments.

Use a slow enough `mock_token_ms` (around 30) that the mock, one Python process,
is not itself the bottleneck at high concurrency.

