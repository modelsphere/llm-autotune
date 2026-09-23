"""The benchmarks AutoTune creates on LLMBench for itself.

A campaign's results are only comparable if the benchmark behind them does not
change mid-campaign. So AutoTune does not borrow a benchmark somebody may edit:
it creates its own from a template (`benchmark_templates/*.yaml`, in LLMBench's
export format), through its LLMBench service account, and locks it.

One rule decides everything: **AutoTune changes only benchmarks it created.**
LLMBench enforces the same rule for a service account, and it is checked here
first so the answer is a clear message rather than a 403:

- the slug does not exist           → import the template, lock it
- it exists and AutoTune created it → use it (lock it if someone unlocked it)
- it exists and someone else did    → refuse; pick another slug
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.evaluation.llmbench import LLMBenchClient

TEMPLATE_DIR = Path(__file__).resolve().parent / "benchmark_templates"
DEFAULT_SCREEN_TEMPLATE = "autotune-screen-v1"
_SLUG = re.compile(r"^[a-z0-9-]+$")


class BenchmarkRefused(RuntimeError):
    """The slug belongs to a benchmark this platform did not create."""


@dataclass
class Ensured:
    slug: str
    benchmark_id: int
    created: bool
    locked: bool


def template_names() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml"))


def load_template(name: str) -> dict:
    path = TEMPLATE_DIR / f"{name}.yaml"
    if not _SLUG.match(name or "") or not path.is_file():
        raise LookupError(f"no benchmark template {name!r} (have: {', '.join(template_names())})")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def ensure_benchmark(
    client: LLMBenchClient, template: str = DEFAULT_SCREEN_TEMPLATE, slug: str = "",
) -> Ensured:
    """Make sure the benchmark exists on LLMBench, is ours, and is locked."""
    doc = load_template(template)
    slug = slug or doc["slug"]
    if not _SLUG.match(slug):
        raise ValueError(f"benchmark slug {slug!r} must be lowercase letters, digits and '-'")
    me = client.whoami()

    existing = client.get_benchmark(slug)
    created = False
    if existing is None:
        doc["slug"] = slug
        existing = client.import_benchmark(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        created = True
    elif existing.get("created_by_user_id") != me.get("id"):
        raise BenchmarkRefused(
            f"benchmark {slug!r} already exists on LLMBench and was not created by this "
            "platform's account; AutoTune will not adopt it — choose another slug"
        )

    if not existing.get("is_locked"):
        existing = client.lock_benchmark(existing["id"])
    return Ensured(slug=slug, benchmark_id=existing["id"], created=created,
                   locked=bool(existing.get("is_locked")))
