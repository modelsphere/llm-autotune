"""AutoTune's own benchmarks on LLMBench — see app/evaluation/benchmarks.py."""

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.auth import get_current_user, require_admin
from app.db.models import User
from app.evaluation.benchmarks import (
    DEFAULT_SCREEN_TEMPLATE,
    BenchmarkRefused,
    ensure_benchmark,
    load_template,
    template_names,
)
from app.evaluation.llmbench import LLMBenchClient

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("/templates", summary="Benchmark templates AutoTune can create on LLMBench")
async def templates(_: User = Depends(get_current_user)):
    out = []
    for name in template_names():
        doc = load_template(name)
        out.append({
            "template": name,
            "slug": doc.get("slug", name),
            "name": doc.get("name", name),
            "description": (doc.get("description") or "").strip(),
            "modules": [m.get("module_name", "") for m in doc.get("modules") or []],
        })
    return out


class EnsureIn(BaseModel):
    template: str = DEFAULT_SCREEN_TEMPLATE
    slug: str = ""          # default: the template's own


@router.post("/ensure", summary="Create (if missing) and lock one of AutoTune's benchmarks")
async def ensure(body: EnsureIn, _: User = Depends(require_admin)):
    """Idempotent: a benchmark AutoTune already created is left as it is (and
    re-locked if someone unlocked it). A slug somebody else owns is refused."""
    try:
        ensured = await anyio.to_thread.run_sync(
            lambda: ensure_benchmark(LLMBenchClient(), body.template, body.slug)
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except BenchmarkRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not reach the benchmark platform: {exc}"
        ) from exc
    return {"slug": ensured.slug, "benchmark_id": ensured.benchmark_id,
            "created": ensured.created, "locked": ensured.locked}
