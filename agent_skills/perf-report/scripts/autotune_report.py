#!/usr/bin/env python3
"""The perf-report skill's one tool: talk to the LLM AutoTune agent API.

    autotune_report.py campaigns
    autotune_report.py campaign <id>
    autotune_report.py compare --baseline s1 --attempts s3,s4 --out work/ [--force]
    autotune_report.py check --markdown work/report.en.md --comparison work/comparison.json
    autotune_report.py save --title T --markdown work/report.en.md
        --comparison work/comparison.json [--lang en] [--translation-of ID] [--labels A,B]

`compare` writes work/comparison.json (the facts) and work/blocks.md (every
chart/table/command block these runs can draw, ready to paste). `check` runs
every save-time check without saving. `save` prints the report id — pass it
as --translation-of when saving the other language.

Environment: AUTOTUNE_URL (the platform API), AUTOTUNE_API_KEY, and optionally
AUTOTUNE_UI_URL (the web UI, for full report links). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


# -- http ------------------------------------------------------------------------


def _base() -> str:
    url = os.environ.get("AUTOTUNE_URL", "").rstrip("/")
    if not url:
        sys.exit("AUTOTUNE_URL is not set (e.g. https://autotune.example.com)")
    return url


def _headers() -> dict[str, str]:
    key = os.environ.get("AUTOTUNE_API_KEY", "")
    if not key:
        sys.exit("AUTOTUNE_API_KEY is not set")
    return {"X-API-Key": key, "Accept": "application/json"}


def _request(method: str, path: str, *, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> Any:
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(body).encode() if body is not None else None
    headers = _headers()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        try:
            detail = json.loads(payload).get("detail", payload)
        except json.JSONDecodeError:
            detail = payload
        raise SystemExit(f"{method} {path} -> {exc.code}\n{json.dumps(detail, indent=2, ensure_ascii=False)}")
    return json.loads(raw) if raw else None


def _get(path: str, **params: Any) -> Any:
    return _request("GET", path, params=params)


# -- commands -------------------------------------------------------------------------


def cmd_campaigns(_: argparse.Namespace) -> None:
    rows = _get("/api/agent/v1/campaigns")
    if not rows:
        print("no campaigns")
        return
    print(f"{'id':>5} {'name':34} {'status':10} {'model':24} {'runs':>4}  ranking metric")
    for c in rows:
        print(f"{c['id']:>5} {c['name'][:34]:34} {c['status']:10} "
              f"{c['model_name'][:24]:24} {c['run_count']:>4}  {c['ranking_metric']}")


def cmd_campaign(args: argparse.Namespace) -> None:
    doc = _get(f"/api/agent/v1/campaigns/{args.id}")
    print(f"{doc['name']}  ({doc['model_name']}, {doc['status']})")
    platform = doc["benchmark"]["platform"]
    if platform.get("gate_text"):
        print(f"gate: {platform['gate_text']}   ranking: {platform.get('ranking_metric', '')}")
    print()
    print(f"{'run':>5} {'status':9} {'engine':18} {'cards':>5} {'headline':>12}  config")
    for r in doc["runs"]:
        engine = f"{r['engine']} {r['engine_version']}".strip()
        head = r["headline"].get("ranking_value")
        base = " (baseline)" if r.get("is_baseline") else ""
        args_text = ", ".join(f"{k}={v}" for k, v in sorted((r["engine_args"] or {}).items()))
        print(f"{r['run_id']:>5} {r['status']:9} {engine[:18]:18} {r['cards']:>5} "
              f"{('—' if head is None else f'{head:,.0f}'):>12}  {args_text[:60]}{base}")
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))


def cmd_compare(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = _get(
        "/api/agent/v1/comparison",
        baseline=args.baseline, attempts=args.attempts,
        force="true" if args.force else None,
        series_metrics=args.series_metrics or None,
    )
    doc.pop("rendered", None)
    (out / "comparison.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out / 'comparison.json'}")
    print(f"baseline: {doc['baseline']['label']} (run {doc['baseline']['run_id']})")
    for a in doc["attempts"]:
        head = a["deltas"]["headline"].get("ranking_value") or {}
        pct = head.get("pct")
        print(f"attempt {a['position']}: {a['label']} (run {a['run_id']}) "
              f"status={a['results']['status']} "
              f"{'Δ ' + format(pct, '+.1f') + '%' if pct is not None else ''}")
    if not doc["comparable"]:
        print("\nNOT COMPARABLE — the report must say so. Reasons:")
        for r in doc["reasons"]:
            print("  -", json.dumps(r, ensure_ascii=False))
    best = doc["best"].get("overall")
    if best:
        print(f"best overall: {best['label']} (run {best['run_id']}) by {best['by']}")
    # A sweep that is marked measured but has no concurrency levels cannot be read or
    # drawn: the platform has the verdict without the data behind it. Say so here so the
    # report names the gap instead of writing bullets around an empty chart.
    for r in [doc["baseline"], *doc["attempts"]]:
        for s in r["results"]["scenarios"]:
            if s["kind"] == "sweep" and not s.get("levels"):
                print(f"DATA GAP: {r['label']} (run {r['run_id']}) scenario {s['key']} "
                      f"({s.get('label')}) has no concurrency levels — report it as missing data, "
                      "do not read numbers into it")
    blocks = _get(
        "/api/agent/v1/report-blocks",
        baseline=args.baseline, attempts=args.attempts,
        force="true" if args.force else None,
    )
    lines = ["# Blocks these runs can draw\n",
             "Paste a block where it belongs in the report. The platform draws it from the",
             "frozen comparison, in the report's language. Do not change the lines inside.\n"]
    for b in blocks:
        lines += [f"## {b['block']}: {b['type']}"
                  + (f" ({', '.join(f'{k}={v}' for k, v in b['params'].items())})" if b["params"] else ""),
                  "", b["description"], "", b["markdown"], ""]
    (out / "blocks.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'blocks.md'} ({len(blocks)} blocks)")


def _report_body(args: argparse.Namespace) -> dict[str, Any]:
    doc = json.loads(Path(args.comparison).read_text(encoding="utf-8"))
    generator = {"agent": "claude-code", "skill": "perf-report"}
    for kv in getattr(args, "generator", None) or []:
        k, _, v = kv.partition("=")
        generator[k] = v
    body: dict[str, Any] = {
        "title": getattr(args, "title", None) or "check",
        "baseline_run_id": doc["baseline"]["run_id"],
        "attempt_run_ids": [a["run_id"] for a in doc["attempts"]],
        "comparable": bool(doc.get("comparable", True)),
        "markdown": Path(args.markdown).read_text(encoding="utf-8"),
        "generator": generator,
        "lang": args.lang,
    }
    if getattr(args, "translation_of", None):
        body["translation_of"] = int(args.translation_of)
    if args.labels:
        body["labels"] = [x.strip() for x in args.labels.split(",")]
    return body


def cmd_check(args: argparse.Namespace) -> None:
    body = _report_body(args)
    path = "/api/agent/v1/reports?" + urllib.parse.urlencode({"dry_run": "true"})
    result = _request("POST", path, body=body)
    print(f"ok: {result['blocks']} blocks resolve, comparable={result['comparable']}")


def cmd_save(args: argparse.Namespace) -> None:
    saved = _request("POST", "/api/agent/v1/reports", body=_report_body(args))
    ui = os.environ.get("AUTOTUNE_UI_URL", "").rstrip("/")
    link = f"{ui}{saved['url']}" if ui else f"{saved['url']} (set AUTOTUNE_UI_URL for a full link)"
    print(f"saved report {saved['id']} ({saved['lang']}): {link}")
    print(f"html export: {_base()}/api/agent/v1/reports/{saved['id']}/export.html")
    print(f"source zip: {_base()}/api/agent/v1/reports/{saved['id']}/bundle.zip")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("campaigns").set_defaults(fn=cmd_campaigns)
    t = sub.add_parser("campaign")
    t.add_argument("id")
    t.add_argument("--json", action="store_true", help="also dump the full document")
    t.set_defaults(fn=cmd_campaign)
    c = sub.add_parser("compare")
    c.add_argument("--baseline", required=True, help="run id")
    c.add_argument("--attempts", required=True, help="comma-separated, in ablation order")
    c.add_argument("--out", default="work")
    c.add_argument("--force", action="store_true", help="accept a not-comparable set (say so in the report)")
    c.add_argument("--series-metrics", default="", help="override the per-level series")
    c.set_defaults(fn=cmd_compare)
    for name, fn in (("check", cmd_check), ("save", cmd_save)):
        x = sub.add_parser(name)
        if name == "save":
            x.add_argument("--title", required=True)
            x.add_argument("--translation-of", default="", help="id of the report this translates")
            x.add_argument("--generator", nargs="*", help="k=v provenance, e.g. model=claude-opus-5")
        x.add_argument("--markdown", required=True)
        x.add_argument("--comparison", required=True)
        x.add_argument("--lang", default="en", choices=("en", "zh"))
        x.add_argument("--labels", default="", help="public config names, baseline first "
                       "(default: Baseline, Optimized)")
        x.set_defaults(fn=fn)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
