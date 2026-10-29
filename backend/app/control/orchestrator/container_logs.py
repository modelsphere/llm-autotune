"""Keeping engine and policy logs, best-effort and bounded.

A container's log lives only as long as the container. Read once at the end,
it is lost whenever that one read fails: the pod was evicted, its node went
away, the worker restarted in between, or the cluster answered with an error
at that moment. So the worker saves the end of every live container's log
every few minutes (`Snapshots`), each save replacing the last, and the final
read at teardown replaces that, unless it comes back empty.
"""

from __future__ import annotations

import re
import time

GAP_MARK = "[... later lines dropped; the end of the log follows ...]"
HEAD_LINES = 800


def clip(text: str, max_bytes: int) -> str:
    """`text` in at most `max_bytes` of UTF-8: its first quarter and its end,
    each cut on a line. The start of an engine's log is where it says what it
    resolved and enabled; the end is what it was doing last — both matter, the
    middle (steady-state batch lines) least."""
    data = text.encode("utf-8")
    if max_bytes <= 0 or len(data) <= max_bytes:
        return text
    head = data[: max_bytes // 4].decode("utf-8", errors="ignore")
    cut = head.rfind("\n")
    head = head[: cut + 1] if cut > 0 else head
    room = max_bytes - len(head.encode("utf-8")) - len(GAP_MARK) - 2
    end = data[-max(room, 0):].decode("utf-8", errors="ignore") if room > 0 else ""
    newline = end.find("\n")
    if 0 <= newline < len(end) - 1:
        end = end[newline + 1:]
    return f"{head}{GAP_MARK}\n{end}"


# A pod's (or container's) section in a multi-pod read: `[name]` alone on a
# line, the way the k8s transports label each pod's log.
_SECTION = re.compile(r"^\[[^\]\n]+\]$", re.MULTILINE)


def collapse_progress(text: str) -> str:
    """Each line as a terminal would show it: a progress bar redraws itself
    with carriage returns, so one line can carry thousands of frames (a
    DeepGEMM warmup did), and only the last one says anything."""
    if "\r" not in text:
        return text
    lines = []
    for line in text.split("\n"):
        frames = [f for f in line.split("\r") if f.strip()]
        lines.append(frames[-1] if frames else "")
    return "\n".join(lines)


def tail_by_section(text: str, max_chars: int) -> str:
    """The end of `text` in at most `max_chars`, each pod's section keeping
    its own end. A multi-pod run (an lws group, pd roles) logs one section
    per pod, one after the other; a plain tail keeps only the last pod, and
    under lws the pod that failed first, and so explains the failure, is
    usually an earlier one."""
    if len(text) <= max_chars:
        return text
    starts = [m.start() for m in _SECTION.finditer(text)]
    if len(starts) < 2:
        return text[-max_chars:]
    if starts[0] > 0:
        starts.insert(0, 0)
    bounds = [*starts, len(text)]
    sections = [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    share = max(max_chars // len(sections) - len(GAP_MARK) - 2, 0)
    kept = []
    for section in sections:
        if len(section) <= share:
            kept.append(section)
            continue
        header, _, body = section.partition("\n")
        if not _SECTION.fullmatch(header):
            header, body = "", section
        room = share - len(header) - 1
        end = body[-room:] if room > 0 else ""
        newline = end.find("\n")
        if 0 <= newline < len(end) - 1:
            end = end[newline + 1:]
        kept.append(f"{header}\n{GAP_MARK}\n{end}" if header else f"{GAP_MARK}\n{end}")
    return "".join(s if s.endswith("\n") else s + "\n" for s in kept).rstrip("\n")


def read_text(path: str | None) -> str:
    """A saved log, or "" when there is none (or it cannot be read)."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def with_head(previous: str, latest: str, head_lines: int = HEAD_LINES) -> str:
    """A log read again: `latest` is only its last lines, so once a container
    has logged more than that its start is gone from the read. Keep the start
    from the earlier save — the first `head_lines` lines — ahead of the latest
    end. A `latest` that still begins where the earlier save began is the whole
    log, and is kept as it is."""
    if not previous.strip():
        return latest
    head = previous.split(GAP_MARK, 1)[0] if GAP_MARK in previous else previous
    head_kept = head.splitlines()[:head_lines]
    if not head_kept:
        return latest
    first = head_kept[0]
    if latest.startswith(first) or latest.splitlines()[:1] == [first]:
        return latest
    return "\n".join(head_kept) + f"\n{GAP_MARK}\n" + latest


class Snapshots:
    """When each live container's log was last saved, so each is read once
    per interval, and the ones waiting longest go first."""

    def __init__(self, per_tick: int = 4) -> None:
        self.per_tick = per_tick
        self._saved_at: dict[tuple[str, int], float] = {}

    def due(
        self, keys: list[tuple[str, int]], interval_seconds: float
    ) -> list[tuple[str, int]]:
        """The keys whose log should be read this tick, oldest first. A key
        seen for the first time starts its clock (a container just launched
        has printed little), and keys no longer live are forgotten."""
        if interval_seconds <= 0:
            return []
        live = set(keys)
        for key in [k for k in self._saved_at if k not in live]:
            del self._saved_at[key]
        now = time.monotonic()
        for key in keys:
            self._saved_at.setdefault(key, now)
        waiting = [k for k in keys if now - self._saved_at[k] >= interval_seconds]
        waiting.sort(key=lambda k: self._saved_at[k])
        return waiting[: self.per_tick]

    def mark(self, key: tuple[str, int]) -> None:
        self._saved_at[key] = time.monotonic()
