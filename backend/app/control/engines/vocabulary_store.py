"""Reading an engine image's vocabulary, and keeping it.

`vocab_probe.py` runs inside the campaign's image, on the campaign's machine,
through the machine's own driver (a Job on k8s, `docker run --rm` over ssh),
so what it reads is exactly what a run of that image would get. One row per
image digest: a moved tag reads again.

The engine's docs are an optional extra on top: sglang describes in its docs
many environment variables its code does not, so when the docs page for the
image's version can be fetched, those descriptions fill the gaps. Nothing
depends on it — no route, a missing page, or an empty URL setting just leaves
those descriptions out, with a note.
"""

import base64
import html
import logging
import re
import threading
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.control.engine_command import PLACEMENT_FLAGS
from app.control.engines.env_keys import placement_env
from app.control.engines.flags import _alias_map
from app.core.config import get_settings

logger = logging.getLogger(__name__)

MARKER = "@@AUTOTUNE_VOCAB@@"
_PROBE = Path(__file__).with_name("vocab_probe.py")
# A failed read is retried after this long, not on every session start.
_RETRY_AFTER = timedelta(hours=1)
# A tag can move to another build; a good read is redone in the background
# once it is this old (the old one serves until the new one lands).
_REFRESH_AFTER = timedelta(days=1)

# What a switch is for, read from its name or its section in the engine's own
# registry or docs: a ROCm or AITER switch does nothing on an NVIDIA card.
_HARDWARE_WORDS: tuple[tuple[str, str], ...] = (
    (r"\b(AMD|ROCM|ROCm|HIP|AITER|MI\d{3}X?)\b|_(ROCM|AITER|HIP)_|^(SGLANG_)?AITER", "amd"),
    (r"\b(NPU|Ascend|ASCEND)\b|_NPU_|_ASCEND_", "npu"),
    (r"\b(Apple|MLX|MPS|Silicon)\b|_MLX_|_MPS_", "apple"),
    (r"\b(XPU|Gaudi|HPU)\b|_XPU_|_HPU_", "intel"),
    (r"\b(TPU|XLA)\b|_TPU_|_XLA_", "tpu"),
)


def probe_command(engine: str) -> str:
    """The shell line that runs the probe with the image's own Python."""
    blob = base64.b64encode(zlib.compress(_PROBE.read_bytes(), 9)).decode()
    code = f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{blob}')))"
    return f'python3 -c "{code}" {engine} || python -c "{code}" {engine}'


def parse_output(text: str) -> dict[str, Any]:
    line = next((ln.strip() for ln in reversed(text.splitlines()) if MARKER in ln), "")
    if not line:
        tail = text.strip()[-800:]
        raise RuntimeError(f"the probe printed no result{': ' + tail if tail else ''}")
    import json

    return json.loads(zlib.decompress(base64.b64decode(line.split(MARKER, 1)[1])))


def hardware_of(*texts: str) -> tuple[str, ...]:
    found = []
    for pattern, tag in _HARDWARE_WORDS:
        if any(t and re.search(pattern, t) for t in texts):
            found.append(tag)
    return tuple(found)


# -- the engine's docs (optional) ----------------------------------------------


def parse_sglang_docs(text: str) -> dict[str, dict[str, str]]:
    """sglang's environment_variables page: one table per `## Section`, rows
    of (name, description, default)."""
    out: dict[str, dict[str, str]] = {}
    section = ""
    cell = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    for chunk in re.split(r"(?m)^(## .+)$", text):
        if chunk.startswith("## "):
            section = chunk[3:].strip()
            continue
        for row in re.findall(r"<tr>(.*?)</tr>", chunk, re.S):
            cells = [_plain(c) for c in cell.findall(row)]
            if len(cells) < 2:
                continue
            names = re.findall(r"\b((?:SGLANG|SGL)_[A-Z0-9_]+)\b", cells[0])
            for name in names:
                out.setdefault(name, {
                    "help": cells[1], "default": cells[2] if len(cells) > 2 else "",
                    "section": section,
                })
    return out


def _plain(cell: str) -> str:
    text = re.sub(r"<[^>]+>", "", cell).replace("`", "")
    return " ".join(html.unescape(text).split())


def fetch_docs(engine: str, version: str) -> tuple[dict[str, dict[str, str]] | None, str]:
    """(descriptions by name, a note) — None when there are none to be had."""
    settings = get_settings()
    template = getattr(settings, f"engine_docs_url_{engine}", "") or ""
    if not template:
        return None, ""
    clean = re.split(r"[+\-]", (version or "").lstrip("v"), maxsplit=1)[0]
    if not clean:
        return None, "engine docs not fetched: the image did not say its version"
    url = template.format(version=clean)
    try:
        response = httpx.get(url, timeout=settings.engine_docs_timeout_seconds,
                             follow_redirects=True)
        response.raise_for_status()
        docs = parse_sglang_docs(response.text) if engine == "sglang" else {}
    except Exception as exc:  # noqa: BLE001 — the docs are an extra, never a failure
        return None, f"engine docs not fetched ({url}): {str(exc)[:200]}"
    return docs, f"{len(docs)} descriptions from the engine's docs for v{clean}"


# -- ingest --------------------------------------------------------------------


def ingest(
    engine: str, payload: dict[str, Any], docs: dict[str, dict[str, str]] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """The probe's entries in the platform's terms: flags under their
    canonical keys (placement left out), env vars without the ones a config
    may never set (secrets, placement), described by the engine's code first
    and its docs where the code says nothing, each tagged with the hardware
    it is for."""
    aliases = _alias_map(engine)
    notes: list[str] = []
    out: dict[tuple[str, str], dict[str, Any]] = {}
    dropped = 0
    for raw in payload.get("entries") or []:
        entry = dict(raw)
        kind, name = entry.get("kind"), str(entry.get("name") or "")
        if kind == "flag":
            name = aliases.get(name, name)
            if name in PLACEMENT_FLAGS:
                continue
            entry.update(name=name, help_source="engine" if entry.get("help") else "")
        elif kind == "env":
            if entry.get("secret") or placement_env(name):
                dropped += 1
                continue
            doc = (docs or {}).get(name)
            if doc and not entry.get("help"):
                entry.update(help=doc["help"], help_source="docs")
                if entry.get("default") in (None, "(computed)") and doc.get("default"):
                    entry["default"] = doc["default"]
            elif entry.get("help") and not entry.get("help_source"):
                entry["help_source"] = "engine"
            if doc and not entry.get("section"):
                entry["section"] = doc["section"]
            entry["hardware"] = list(hardware_of(
                name, entry.get("section") or "", (doc or {}).get("section", "")
            ))
        else:
            continue
        if (kind, name) in out:
            # A deprecated spelling of a flag already read ("--cuda-graph-max-bs"
            # for --cuda-graph-max-bs-decode): the engine still takes it.
            if kind == "flag" and entry.get("flag"):
                out[(kind, name)].setdefault("deprecated_flags", []).append(entry["flag"])
            continue
        out[(kind, name)] = entry
    if dropped:
        notes.append(f"{dropped} env vars left out: credentials or placement")
    return list(out.values()), notes


# -- reading an image, and keeping what it said -----------------------------------


def needs_reading(db, engine: str, image: str) -> bool:
    """No vocabulary for the image, a failed read over an hour old, or a good
    one over a day old (its tag may point at another build by now)."""
    from app.db.models import EngineVocabulary

    row = db.execute(
        select(EngineVocabulary)
        .where(EngineVocabulary.engine == engine, EngineVocabulary.image == image)
        .order_by(EngineVocabulary.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return True
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created
    return age > (_RETRY_AFTER if row.status == "failed" else _REFRESH_AFTER)


def read_image(driver, machine, engine: str, image: str) -> dict[str, Any]:
    """Run the probe in the image and return the row's fields. Raises
    LaunchDeferred when the image is not on the machine yet."""
    from app.control.launch.base import LaunchDeferred

    settings = get_settings()
    try:
        text, digest = driver.run_in_image(
            machine, image, probe_command(engine), settings.engine_vocab_timeout_seconds
        )
        payload = parse_output(text)
    except LaunchDeferred:
        raise
    except Exception as exc:  # noqa: BLE001 — recorded as a failed read
        return {"engine": engine, "image": image, "status": "failed", "entries": [],
                "notes": [f"could not read the image: {str(exc)[:1500]}"]}
    docs, doc_note = (None, "")
    if any(e.get("kind") == "env" for e in payload.get("entries") or []):
        docs, doc_note = fetch_docs(engine, payload.get("engine_version") or "")
    entries, notes = ingest(engine, payload, docs)
    return {
        "engine": engine, "image": image, "image_digest": digest[:128],
        "engine_version": str(payload.get("engine_version") or "")[:64],
        "status": payload.get("status") or "ok", "entries": entries,
        "notes": [*(payload.get("notes") or []), *notes, *([doc_note] if doc_note else [])],
    }


def needs_model_reading(db, model_path: str) -> bool:
    """No config read for the model path, or a failed read over an hour old.
    The weights at a path do not change under a campaign, so a good read stays."""
    from app.db.models import ModelConfig

    row = db.execute(
        select(ModelConfig).where(ModelConfig.model_path == model_path)
        .order_by(ModelConfig.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return True
    if row.status != "failed":
        return False
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > _RETRY_AFTER


_reading: set[tuple[str, str]] = set()
_lock = threading.Lock()


def in_background(session_factory, key: tuple[str, str], produce) -> bool:
    """Run `produce()` on a thread — it reads an image or a model and returns
    the row to keep — once at a time per key; False when one is running."""
    with _lock:
        if key in _reading:
            return False
        _reading.add(key)

    def work() -> None:
        from app.control.launch.base import LaunchDeferred

        try:
            row = produce()
            with session_factory() as db:
                db.add(row)
                db.commit()
            logger.info("read %s %s: %s", key[0], key[1], row.status)
        except LaunchDeferred as exc:
            logger.info("read of %s %s deferred: %s", key[0], key[1], exc)
        except Exception:  # noqa: BLE001 — a background read never takes the worker down
            logger.exception("read of %s %s failed", key[0], key[1])
        finally:
            with _lock:
                _reading.discard(key)

    threading.Thread(target=work, name=f"read-{key[0]}", daemon=True).start()
    return True
