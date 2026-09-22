"""Report blocks: what a report's markdown may ask the renderer to draw.

A report's charts and tables are not in its markdown. The agent writes a
fenced block that names one, and the report page (and the HTML export) draws
it from the comparison frozen with the report:

    ```chart
    type: sweep
    scenario: perf_guidellm_sweep#2
    ```

So a number on a report can never be a typo, restyling a chart restyles every
report, and the agent's job is the prose and the order of things.

This module is the contract's server half: which blocks exist (`available`,
served to the agent ready to paste) and whether a report's blocks all resolve
against its comparison (`problems`, checked on save). The drawing half lives
in the frontend (`src/report/`); the two agree on the names below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.agent import ComparisonDocument, ReportBlock

BLOCK_KINDS = ("chart", "table", "command")
CHART_TYPES = ("summary", "sweep", "agentic")
TABLE_TYPES = ("setup", "summary", "quality", "diff", "scenarios")

_FENCE = re.compile(r"^```[ \t]*(chart|table|command)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_LINE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*?)\s*$")


@dataclass
class Block:
    kind: str
    params: dict[str, str] = field(default_factory=dict)
    line: int = 0

    @property
    def type(self) -> str:
        return self.params.get("type", "")


def parse(markdown: str) -> list[Block]:
    """Every chart/table/command block in the markdown, in order. A body line
    that is not `key: value` is kept under `_bad` so the check can name it."""
    blocks = []
    for m in _FENCE.finditer(markdown):
        params: dict[str, str] = {}
        for raw in m.group(2).splitlines():
            if not raw.strip():
                continue
            kv = _LINE.match(raw)
            if kv:
                params[kv.group(1).lower()] = kv.group(2)
            else:
                params.setdefault("_bad", raw.strip())
        blocks.append(Block(m.group(1), params, markdown.count("\n", 0, m.start()) + 1))
    return blocks


def _snippet(kind: str, params: dict[str, str]) -> str:
    body = "\n".join(f"{k}: {v}" for k, v in params.items())
    return f"```{kind}\n{body}\n```"


def available(doc: ComparisonDocument) -> list[ReportBlock]:
    """Every block this comparison can draw, each with the markdown to paste."""
    out: list[ReportBlock] = []

    def add(kind: str, params: dict[str, str], description: str) -> None:
        out.append(ReportBlock(block=kind, type=params.get("type", "serve"),
                               params={k: v for k, v in params.items() if k != "type"},
                               description=description, markdown=_snippet(kind, params)))

    add("chart", {"type": "summary"},
        "Bars: total throughput per GPU at the best concurrency within SLO, per scenario, "
        "baseline next to each attempt, change on the bar.")
    for s in doc.baseline.results.scenarios:
        if s.kind == "replay":
            add("chart", {"type": "agentic", "scenario": s.key},
                "Agentic dataset: input (uncached/cached) and output throughput per GPU, "
                "TTFT p50/p90/p99.")
        else:
            add("chart", {"type": "sweep", "scenario": s.key},
                f"Sweep {s.label}: throughput per GPU and TTFT against concurrency, SLO line, "
                "best concurrency within SLO marked.")
    add("table", {"type": "setup"}, "Model, precision, hardware, image, SLO.")
    add("table", {"type": "summary"},
        "Per scenario: throughput per GPU at the best concurrency within SLO, with the change.")
    add("table", {"type": "quality"}, "Quality scores held to a floor, with the change.")
    for a in doc.attempts:
        add("table", {"type": "diff", "attempt": str(a.position)},
            f"Launch settings that differ between the baseline and attempt {a.position}.")
    add("table", {"type": "scenarios"}, "What each scenario runs and how it is measured.")
    add("command", {"config": "baseline"}, "The baseline's serving command, with a copy button.")
    for a in doc.attempts:
        add("command", {"config": str(a.position)},
            f"Attempt {a.position}'s serving command, with a copy button.")
    return out


def problems(markdown: str, doc: ComparisonDocument) -> list[dict[str, object]]:
    """Why a report's blocks would not draw, one entry per bad block; [] when
    they all resolve. Each entry names the line and what would have worked."""
    scenarios = {s.key: s for s in doc.baseline.results.scenarios}
    positions = {str(a.position) for a in doc.attempts}
    out: list[dict[str, object]] = []

    def bad(b: Block, message: str) -> None:
        out.append({"line": b.line, "block": b.kind, "params": b.params, "problem": message})

    for b in parse(markdown):
        if "_bad" in b.params:
            bad(b, f"not a `key: value` line: {b.params['_bad']!r}")
            continue
        if b.kind == "chart":
            if b.type not in CHART_TYPES:
                bad(b, f"chart type must be one of {', '.join(CHART_TYPES)}")
            elif b.type in ("sweep", "agentic"):
                key = b.params.get("scenario", "")
                s = scenarios.get(key)
                want = "replay" if b.type == "agentic" else "sweep"
                if s is None:
                    bad(b, f"no scenario {key!r}; have {', '.join(scenarios) or 'none'}")
                elif s.kind != want:
                    bad(b, f"scenario {key!r} is a {s.kind}, not a {want}")
        elif b.kind == "table":
            if b.type not in TABLE_TYPES:
                bad(b, f"table type must be one of {', '.join(TABLE_TYPES)}")
            elif b.type == "diff" and b.params.get("attempt", "1") not in positions:
                have = ", ".join(sorted(positions))
                bad(b, f"no attempt {b.params.get('attempt')!r}; have {have}")
        elif b.kind == "command":
            config = b.params.get("config", "")
            if config != "baseline" and config not in positions:
                bad(b, "config must be `baseline` or an attempt position "
                       f"({', '.join(sorted(positions))})")
    return out
