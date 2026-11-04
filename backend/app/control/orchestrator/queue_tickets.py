"""The machine queue's tickets: each waiter's place in line, kept while it waits.

Supervisor._schedule serves waiters oldest first (machine_queue.py says who
waits and since when). A waiter's place used to be worked out again on every
pass, so nothing recorded it and nothing showed it. Now the first pass that
sees a waiter gives it a ticket, ordered by that same key (arrival, rank,
ident), frozen at issue; it keeps the ticket until it is served, and a
waiter that comes back after being served queues again at the back, as
before.

What a ticket orders is everything a run needs before it may be placed:
cards on a machine, and, on a cluster with one fixed NodePort, that port.
Both are handed out at placement, by the same pass, in ticket order, and a
waiter that cannot be served holds every machine it could use, and every
machine that shares its port, against all later tickets (Supervisor._schedule).
So the port cannot go to a later ticket while an earlier one waits for cards,
nor cards to a later one while an earlier one waits for the port.

On every pass the scheduler writes what each waiter is waiting for, why, and
its place among the waiters for the same thing, for the campaign page.

Robustness:
- A ticket is a cache of the order, never the authority on who waits: the
  waiters are listed afresh each pass, served ones give theirs up at once,
  and one not seen for `GRACE` (it left: done, paused, gated by its window
  or dataset) is let go. A plugin whose listing failed keeps its tickets.
- Losing every ticket costs a waiter nothing it did not have before: a
  fresh one is taken in the old order (arrival from the same key).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.orchestrator.lifecycle import as_utc, now
from app.db.models import QueueTicket

# How long a ticket outlives the last pass that saw its waiter waiting.
GRACE = timedelta(minutes=10)
# A ticket seen this recently is current: its waiter is in the queue now.
FRESH = timedelta(minutes=2)


@dataclass
class Entry:
    """One waiter in one pass, with the ticket that orders it."""

    ticket: QueueTicket
    kind: str  # "campaign", "plugin", "session"
    item: object
    # Whether it needs a cluster's fixed NodePort as well as cards: a run it
    # places does. A policy session's reservation takes cards only; its own
    # launches queue for the port as runs (node_ports.port_holder).
    needs_port: bool = True
    served: bool = False
    busy: set[int] = field(default_factory=set)
    held: set[int] = field(default_factory=set)


class TicketBook:
    """The tickets of one pass: take, order, record, let go."""

    def __init__(self, session: Session):
        self.session = session
        self.now = now()
        self.tickets = {t.waiter: t for t in session.scalars(select(QueueTicket)).all()}
        self._kept_prefixes: set[str] = set()

    def take(
        self, waiter: str, key: tuple, *, label: str, campaign_id: int | None = None
    ) -> QueueTicket:
        """The waiter's ticket, issued now if it has none, marked seen."""
        ticket = self.tickets.get(waiter)
        if ticket is None:
            arrival, rank, ident = key
            ticket = QueueTicket(
                waiter=waiter, label=label[:255], campaign_id=campaign_id,
                arrival=as_utc(arrival) or self.now, rank=int(rank), ident=int(ident),
                issued_at=self.now, seen_at=self.now,
            )
            self.session.add(ticket)
            self.tickets[waiter] = ticket
        else:
            ticket.seen_at = self.now
            if ticket.label != label[:255]:
                ticket.label = label[:255]
        return ticket

    def keep(self, prefix: str) -> None:
        """Waiters under this prefix could not be listed this pass: their
        tickets are not let go for being unseen."""
        self._kept_prefixes.add(prefix)

    def order(self, entries: list[Entry]) -> list[Entry]:
        self.session.flush()  # new tickets get their id, the last tie-break
        return sorted(
            entries,
            key=lambda e: (as_utc(e.ticket.arrival), e.ticket.rank, e.ticket.ident, e.ticket.id),
        )

    def served(self, entry: Entry) -> None:
        """Served: the ticket is given up. A waiter with more to place takes
        a new one, at the back, on the next pass."""
        if not entry.served:
            entry.served = True
            self.tickets.pop(entry.ticket.waiter, None)
            self.session.delete(entry.ticket)

    def record(self, entry: Entry, *, position: int, waiting_for: str, reason: str) -> None:
        ticket = entry.ticket
        if ticket.position != position:
            ticket.position = position
        if ticket.waiting_for != waiting_for:
            ticket.waiting_for = waiting_for
        if ticket.reason != reason[:2000]:
            ticket.reason = reason[:2000]

    def sweep(self) -> None:
        """Let go of tickets whose waiter has not been seen waiting for GRACE."""
        cutoff = self.now - GRACE
        for waiter, ticket in list(self.tickets.items()):
            if any(waiter.startswith(p) for p in self._kept_prefixes):
                continue
            if (as_utc(ticket.seen_at) or self.now) < cutoff:
                self.session.delete(ticket)
                del self.tickets[waiter]


def place_in_line(entries: list[Entry]) -> dict[int, int]:
    """Each unserved waiter's place among those waiting for the same thing:
    1 + how many earlier unserved waiters hold any machine it holds. Entries
    in ticket order; keyed by ticket id."""
    out: dict[int, int] = {}
    waiting = [e for e in entries if not e.served and e.held]
    for i, entry in enumerate(waiting):
        ahead = sum(1 for other in waiting[:i] if other.held & entry.held)
        out[entry.ticket.id] = ahead + 1
    return out
