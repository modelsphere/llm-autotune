"""Mock LLM inference engine — stdlib only.

Speaks just enough OpenAI API for the autotune flow (/v1/models, /health,
/v1/chat/completions) and accepts the same command lines the real engines do,
so the ssh_docker driver launches it unmodified:

    python3 -m sglang.launch_server --model-path /model --port 30000 ...
    vllm serve /model --served-model-name m --port 30000 ...

Unknown flags (tp-size, mem-fraction-static, ...) are accepted and ignored.
Behavior knobs are ordinary flags, so campaigns control them via
search_space.base / grid:

    --mock-startup-seconds N    weight-loading delay before the port opens (default 3)
    --mock-fail MODE            oom | exit — crash during startup with matching logs
    --mock-fail-after-seconds N crash (OOM) N seconds after becoming ready
    --mock-latency-ms N         per-completion latency (default 200)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    parser.add_argument("--mock-startup-seconds", type=float, default=3.0)
    parser.add_argument("--mock-fail", default="")
    parser.add_argument("--mock-fail-after-seconds", type=float, default=0.0)
    parser.add_argument("--mock-latency-ms", type=float, default=200.0)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"[mock-engine] ignoring flags: {unknown}", flush=True)
    return args


class Handler(BaseHTTPRequestHandler):
    served_model_name = "mock-model"
    latency_ms = 200.0
    request_count = 0

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
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, status=404)
            return
        try:
            request_body = json.loads(raw)
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, status=400)
            return
        Handler.request_count += 1
        time.sleep(self.latency_ms / 1000)
        prompt = ""
        messages = request_body.get("messages") or []
        if messages:
            prompt = str(messages[-1].get("content", ""))[:80]
        content = f"OK (mock reply from {self.served_model_name}; prompt was: {prompt!r})"
        self._json(
            {
                "id": f"mock-{Handler.request_count}",
                "object": "chat.completion",
                "model": self.served_model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 16, "completion_tokens": 24, "total_tokens": 40},
            }
        )

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

    # Simulate weight loading: the port opens only after the delay, exactly the
    # STARTING window the supervisor sees on real launches.
    print(f"[mock-engine] loading weights ({args.mock_startup_seconds}s)...", flush=True)
    time.sleep(args.mock_startup_seconds)

    Handler.served_model_name = args.served_model_name
    Handler.latency_ms = args.mock_latency_ms
    server = ThreadingHTTPServer((args.host, args.port), Handler)

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
