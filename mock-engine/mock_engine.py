"""Mock LLM inference engine — stdlib only.

Speaks just enough OpenAI API for the autotune flow (/v1/models, /health,
/v1/chat/completions, /v1/completions) and accepts the same command lines the
real engines do, so the launch drivers start it unmodified:

    python3 -m sglang.launch_server --model-path /model --port 30000 ...
    vllm serve /model --served-model-name m --port 30000 ...

Unknown flags (tp-size, mem-fraction-static, ...) are accepted and ignored.
Behavior knobs are ordinary flags, so campaigns control them via
search_space.base / grid:

    --mock-startup-seconds N    weight-loading delay before the port opens (default 1)
    --mock-fail MODE            oom | exit — crash during startup with matching logs
    --mock-fail-after-seconds N crash (OOM) N seconds after becoming ready
    --mock-latency-ms N         time to the first token (default 20)
    --mock-token-ms N           time between streamed tokens (default 1)
    --mock-max-output-tokens N  cap on tokens per reply (default 256)
    --mock-landscape sglang     let real sglang flags move the numbers (landscape.py):
                                KV capacity, batch limits, CUDA graphs, attention
                                backend, OOM edges — something for a search to find

Streaming is real: `"stream": true` answers server-sent events, one token per
chunk, so a benchmark that times the first token and the gaps between tokens
(guidellm, LLMBench's sweeps) measures the same shape it would on an engine —
just with numbers that mean nothing about any model.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from landscape import Landscape, StartupError, parse_flags

OOM_LOG = (
    "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.50 GiB "
    "(GPU 0; 79.10 GiB total capacity)"
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model-path", default="/model")
    parser.add_argument("--served-model-name", default="mock-model")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=30000)
    parser.add_argument("--mock-startup-seconds", type=float, default=1.0)
    parser.add_argument("--mock-fail", default="")
    parser.add_argument("--mock-fail-after-seconds", type=float, default=0.0)
    parser.add_argument("--mock-latency-ms", type=float, default=20.0)
    parser.add_argument("--mock-token-ms", type=float, default=1.0)
    parser.add_argument("--mock-max-output-tokens", type=int, default=256)
    parser.add_argument("--mock-landscape", default="")
    args, unknown = parser.parse_known_args(argv)
    args.engine_flags = unknown
    if unknown and not args.mock_landscape:
        print(f"[mock-engine] ignoring flags: {unknown}", flush=True)
    return args


def _prompt_tokens(body: dict) -> int:
    """A rough count, so `usage.prompt_tokens` scales with the request the way
    a benchmark's synthetic prompts expect (about four characters a token)."""
    if isinstance(body.get("prompt"), str):
        text = body["prompt"]
    else:
        text = " ".join(
            str(m.get("content", "")) for m in (body.get("messages") or []) if isinstance(m, dict)
        )
    return max(1, len(text) // 4)


# The answers to the health gate's sanity questions (a copy of the backend's
# app/evaluation/data/gsm8k_sanity.jsonl; a test keeps the two the same). A
# mock cannot do arithmetic, but it must pass the gate that real engines pass.
_ANSWERS = {
    row["question"]: row["answer"]
    for row in (
        json.loads(line)
        for line in (Path(__file__).parent / "gsm8k_sanity.jsonl").read_text().splitlines()
        if line.strip()
    )
}


def _known_answer(body: dict) -> int | None:
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            text = str(message.get("content", ""))
            return next((a for q, a in _ANSWERS.items() if text.startswith(q)), None)
    return None


class Handler(BaseHTTPRequestHandler):
    served_model_name = "mock-model"
    latency_ms = 200.0
    token_ms = 5.0
    max_output_tokens = 256
    request_count = 0
    landscape: Landscape | None = None

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            self._json(
                {
                    "object": "list",
                    "data": [
                        {"id": self.served_model_name, "object": "model", "owned_by": "mock"}
                    ],
                }
            )
        elif self.path in ("/health", "/health_generate"):
            self._json({"status": "ok"})
        elif self.path == "/get_server_info" and self.landscape is not None:
            self._json({**self.landscape.args, "max_total_num_tokens": self.landscape.kv_capacity,
                        "served_model_name": self.served_model_name})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        chat = self.path == "/v1/chat/completions"
        if not chat and self.path != "/v1/completions":
            self._json({"error": "not found"}, status=404)
            return
        try:
            request_body = json.loads(raw)
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, status=400)
            return
        Handler.request_count += 1
        request_id = f"mock-{Handler.request_count}"

        # The reply is as long as the caller asked for (max_tokens), capped, so
        # a sweep that requests 512 output tokens gets 256 streamed tokens and a
        # throughput number, not a 24-token blip.
        want = request_body.get("max_tokens") or request_body.get("max_completion_tokens")
        n_tokens = min(int(want) if want else 24, self.max_output_tokens)
        n_tokens = max(1, n_tokens)
        prompt_tokens = _prompt_tokens(request_body)
        # A reply that reads as one ("OK (mock reply from …)"), then filler
        # tokens up to the requested length.
        lead = ["OK ", "(mock ", "reply ", "from ", f"{self.served_model_name}) "]
        answer = _known_answer(request_body)
        if answer is not None:
            lead = ["The ", "answer ", "is:\n", f"#### {answer}"]
            n_tokens = min(n_tokens, len(lead))
        tokens = (lead + [f"tok{i} " for i in range(len(lead), n_tokens)])[:n_tokens]
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": n_tokens,
            "total_tokens": prompt_tokens + n_tokens,
        }

        landscape = self.landscape
        if landscape is not None:
            footprint = prompt_tokens + n_tokens
            landscape.admit(footprint)
            try:
                landscape.prefill(prompt_tokens)
                if request_body.get("stream"):
                    include_usage = bool((request_body.get("stream_options") or {}).get("include_usage"))
                    self._stream(request_id, chat, tokens, usage if include_usage else None)
                else:
                    for _ in range(n_tokens - 1):
                        time.sleep(landscape.step_ms() / 1000)
                        landscape.token_out()
                    self._reply(request_id, chat, tokens, usage)
            finally:
                landscape.release(footprint)
            return

        time.sleep(self.latency_ms / 1000)  # "prefill": the time to the first token
        if request_body.get("stream"):
            include_usage = bool((request_body.get("stream_options") or {}).get("include_usage"))
            self._stream(request_id, chat, tokens, usage if include_usage else None)
            return

        time.sleep(self.token_ms / 1000 * (n_tokens - 1))  # "decode", all at once
        self._reply(request_id, chat, tokens, usage)

    def _reply(self, request_id: str, chat: bool, tokens: list[str], usage: dict) -> None:
        content = "".join(tokens).rstrip()
        if chat:
            choice = {"index": 0, "message": {"role": "assistant", "content": content},
                      "finish_reason": "stop"}
        else:
            choice = {"index": 0, "text": content, "finish_reason": "stop"}
        self._json({
            "id": request_id,
            "object": "chat.completion" if chat else "text_completion",
            "created": int(time.time()),
            "model": self.served_model_name,
            "choices": [choice],
            "usage": usage,
        })

    def _stream(self, request_id: str, chat: bool, tokens: list[str], usage: dict | None) -> None:
        """Server-sent events, one token per chunk, `data: [DONE]` last — the
        wire format the OpenAI clients and guidellm parse."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta: dict | None, finish: str | None, with_usage: dict | None = None) -> bytes:
            if chat:
                choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
            else:
                choice = {"index": 0, "text": (delta or {}).get("content", ""),
                          "finish_reason": finish}
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk" if chat else "text_completion",
                "created": int(time.time()),
                "model": self.served_model_name,
                "choices": [] if delta is None and finish is None else [choice],
            }
            if with_usage is not None:
                payload["usage"] = with_usage
            return f"data: {json.dumps(payload)}\n\n".encode()

        try:
            if chat:
                self.wfile.write(chunk({"role": "assistant", "content": ""}, None))
            for i, tok in enumerate(tokens):
                if i:
                    if self.landscape is not None:
                        time.sleep(self.landscape.step_ms() / 1000)
                        self.landscape.token_out()
                    else:
                        time.sleep(self.token_ms / 1000)
                self.wfile.write(chunk({"content": tok}, None))
                self.wfile.flush()
            self.wfile.write(chunk({}, "stop"))
            if usage is not None:
                self.wfile.write(chunk(None, None, usage))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up mid-reply; a benchmark counts that itself

    def log_message(self, fmt, *log_args):
        print(f"[mock-engine] {self.address_string()} {fmt % log_args}", flush=True)


def main(mode: str = "sglang", argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if mode == "vllm":
        # `vllm serve MODEL --flags...` — swallow the subcommand + positional
        if argv and argv[0] == "serve":
            argv = argv[1:]
        if argv and not argv[0].startswith("-"):
            argv = ["--model-path", argv[0], *argv[1:]]
    args = parse_args(argv)

    print(f"[mock-engine] {mode} mock starting; model={args.model_path} "
          f"served={args.served_model_name} port={args.port}", flush=True)

    if args.mock_fail == "oom":
        print("[mock-engine] loading weights...", flush=True)
        time.sleep(min(args.mock_startup_seconds, 2))
        print(OOM_LOG, file=sys.stderr, flush=True)
        sys.exit(1)
    if args.mock_fail == "exit":
        print("[mock-engine] fatal: simulated startup failure", file=sys.stderr, flush=True)
        sys.exit(1)

    landscape = None
    if args.mock_landscape:
        try:
            landscape = Landscape(parse_flags(args.engine_flags), args.mock_token_ms)
        except StartupError as exc:
            print("[mock-engine] loading weights...", flush=True)
            time.sleep(min(args.mock_startup_seconds, 2))
            print(str(exc), file=sys.stderr, flush=True)
            sys.exit(1)
        print(landscape.server_args_line(), flush=True)

    # Simulate weight loading: the port opens only after the delay, exactly the
    # STARTING window the supervisor sees on real launches.
    startup = args.mock_startup_seconds + (landscape.startup_extra if landscape else 0.0)
    print(f"[mock-engine] loading weights ({startup}s)...", flush=True)
    time.sleep(startup)

    Handler.served_model_name = args.served_model_name
    Handler.latency_ms = args.mock_latency_ms
    Handler.token_ms = args.mock_token_ms
    Handler.max_output_tokens = max(1, args.mock_max_output_tokens)
    Handler.landscape = landscape
    if landscape is not None:
        landscape.start_logger()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    if args.mock_fail_after_seconds > 0:
        def _delayed_crash():
            time.sleep(args.mock_fail_after_seconds)
            print(OOM_LOG, file=sys.stderr, flush=True)
            server.shutdown()
        threading.Thread(target=_delayed_crash, daemon=True).start()

    print(f"[mock-engine] ready on {args.host}:{args.port}", flush=True)
    server.serve_forever()
    # only reached via _delayed_crash's shutdown()
    sys.exit(1)


if __name__ == "__main__":
    main()
