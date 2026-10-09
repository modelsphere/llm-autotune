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

from app.core.config import get_settings
from app.evaluation.llmbench import LLMBenchClient

TEMPLATE_DIR = Path(__file__).resolve().parent / "benchmark_templates"
DEFAULT_SCREEN_TEMPLATE = "autotune-screen-v1"
_SLUG = re.compile(r"^[a-z0-9-]+$")
# LLMBench's list groups benchmarks by tag path; AutoTune's all go under this.
GROUP_TAG = "llm-autotune"


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
    return ensure_document(client, doc, slug or doc["slug"])


def ensure_document(client: LLMBenchClient, doc: dict, slug: str) -> Ensured:
    """Create `doc` on LLMBench as `slug` unless AutoTune already has, then
    lock it. Everything AutoTune creates is filed under its group tag, so it
    sits in one place in LLMBench's list instead of among everyone's."""
    if not _SLUG.match(slug):
        raise ValueError(f"benchmark slug {slug!r} must be lowercase letters, digits and '-'")
    me = client.whoami()
    tags = list(doc.get("group_tags") or [GROUP_TAG])

    existing = client.get_benchmark(slug)
    created = False
    if existing is None:
        doc = {**doc, "slug": slug, "group_tags": tags}
        existing = client.import_benchmark(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        created = True
    elif existing.get("created_by_user_id") != me.get("id"):
        raise BenchmarkRefused(
            f"benchmark {slug!r} already exists on LLMBench and was not created by this "
            "platform's account; AutoTune will not adopt it — choose another slug"
        )
    elif not existing.get("group_tags"):
        # Created before AutoTune filed its benchmarks. Grouping is presentation
        # only, so LLMBench takes it on a locked benchmark; a refusal leaves the
        # benchmark usable, just unfiled.
        try:
            existing = client.set_group_tags(existing["id"], tags)
        except Exception:
            pass

    if not existing.get("is_locked"):
        existing = client.lock_benchmark(existing["id"])
    return Ensured(slug=slug, benchmark_id=existing["id"], created=created,
                   locked=bool(existing.get("is_locked")))


def file_untagged(client: LLMBenchClient) -> list[str]:
    """File every benchmark this account created but never tagged — made
    before AutoTune filed its benchmarks — under its group. Best-effort per
    benchmark; returns the slugs filed."""
    me = client.whoami().get("id")
    filed = []
    for benchmark in client.list_benchmarks():
        if benchmark.get("created_by_user_id") != me or benchmark.get("group_tags"):
            continue
        try:
            client.set_group_tags(benchmark["id"], [GROUP_TAG])
            filed.append(benchmark["slug"])
        except Exception:
            continue
    return filed


def ensure_screen_benchmark(max_attempts: int = 1) -> Ensured | None:
    """Ensure the default screen benchmark on the configured LLMBench, and
    file any of AutoTune's older benchmarks under its group.

    None when there is nothing to do: ensuring is turned off, or no LLMBench
    is configured. Raises when LLMBench cannot be reached or refuses."""
    settings = get_settings()
    if not settings.llmbench_ensure_benchmarks or not settings.llmbench_base_url:
        return None
    client = LLMBenchClient(max_attempts=max_attempts)
    ensured = ensure_benchmark(client)
    file_untagged(client)
    return ensured
