# Machine Lease API

How an external system hands GPU machines to LLM Autotune and takes them back.

Interactive reference (always matches the running build): **`/api/docs`** —
e.g. <http://192.0.2.10:28100/api/docs>. This document is the narrative
version: what the calls mean, and what the platform guarantees.

---

## The contract in one paragraph

You lend the platform a machine for a period. While it holds the machine it
will record what production is running on it, benchmark that as a control,
stop it, run experiments, and **put production back before giving the machine
up** — whether the lease ends on schedule, early, or abruptly. You take the
machine back by asking; the machine is genuinely yours when its `readiness`
reads `returnable`.

---

## Authentication

Mint a key in the UI (**API keys** page) or via `POST /api/api-keys`, then send
it on every request:

```
X-API-Key: atk_1a2b3c4d_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The plaintext key is shown **once**, at creation. Only its hash is stored, so a
lost key is replaced rather than recovered. Everything a key does is attributed
in the audit trail as `key:<name>` — name your keys after the system that holds
them.

A browser session (`Authorization: Bearer …`) works on these endpoints too, so
you can try any of them from `/api/docs` while logged in.

---

## Lending a machine

```http
POST /api/machines/lease
Content-Type: application/json
X-API-Key: atk_…

{
  "name": "node-24",
  "host": "198.51.100.24",
  "ssh_user": "root",
  "gpu_count": 8,
  "gpu_type": "A100-SXM4-80GB",
  "due_at": "2026-08-05T00:00:00Z",
  "lease_note": "nightly tuning slot"
}
```

`name` is the identity. The first call registers the machine; every later call
updates its details and starts a fresh lease. **Retrying is safe** — a caller
that crashes mid-request and repeats it gets the same outcome, and never has to
ask "did that one land?".

| Field | Notes |
| --- | --- |
| `name` | Required. Your fleet's name for the box. |
| `host` | Required the first time. Must be reachable **both** from the platform (ssh) and from the benchmark platform (HTTP) — the endpoint we hand to LLMBench is built from it. |
| `gpu_count` | Cards the platform may schedule onto. |
| `due_at` | When you want it back. Reaching it starts a polite hand-back **on its own** — a lease nobody ends still returns. Defaults to 24 h. |

What happens next needs no further calls from you:

```
capture  →  benchmark production  →  stop production  →  experiments  →  restore
```

Production is stopped only after the control benchmark **passes**. A machine we
could not measure is one we do not touch — it keeps serving, and the campaign
reports the failure instead.

---

## Asking how it is going

```http
GET /api/machines/node-24/lease
```

```json
{
  "machine": "node-24",
  "state": "reserved",
  "lease_state": "active",
  "lease_holder": "key:fleet-manager",
  "lease_due_at": "2026-08-05T00:00:00Z",
  "readiness": "busy",
  "returnable_at": "2026-08-04T18:41:00Z",
  "production_status": "cleared",
  "gpus_in_use": 4,
  "gpu_count": 8,
  "live_runs": [
    {"run_id": 88, "campaign_id": 21, "status": "benching",
     "gpus": [0, 1], "started_at": "2026-08-04T16:11:00Z"}
  ],
  "stage": {"headline": "2 run(s) in flight", "detail": "…", "step": 3, "state": "working"}
}
```

**`readiness` is the field to poll.** It answers the only question an external
scheduler has:

| Value | Meaning |
| --- | --- |
| `busy` | Our runs are on it. `returnable_at` is the worst case for when they end. |
| `idle` | Ours, nothing running — but production may still be stopped, so **not yet yours**. |
| `returnable` | Production is back up. Take the machine whenever you like. |

`returnable_at` is a **bound, not an estimate**: each live run's own cutoff
(`started_at + max_run_minutes`), so you can plan against it. A typical run is
far shorter.

`GET /api/machines/lease` returns the same shape for every machine at once.

---

## Taking it back

```http
POST /api/machines/node-24/lease/end
{"mode": "polite", "deadline_seconds": 1800, "reason": "capacity needed"}
```

This returns immediately. It records the request; the worker carries it out.

### `polite` — don't waste the work

No new runs start. Runs already in flight finish normally and their
measurements are kept. Use `returnable_at` from the status call to see the
worst case — up to a campaign's `max_run_minutes`, typically ~20 minutes.

### `eager` — the machine matters more than the data

Live runs are killed on the next worker tick (~10 s) and their benchmarks are
lost. Production is still restored; eager kills the experiments, not the
hand-back.

### `deadline_seconds`

An upper bound on the whole operation. On a **polite** end this is the point at
which waiting turns into killing — so `{"mode": "polite", "deadline_seconds":
1800}` reads as *"let them finish, but be off within half an hour either way."*
Omit it for no hard stop.

### Escalating

Calling `end` again on a draining machine is allowed and is how you change your
mind: send `polite` first, `eager` when it turns out you needed it sooner.

---

## Knowing when it is done

Ending a lease is **asynchronous**. Poll until `readiness` is `returnable`:

```bash
until [ "$(curl -sf -H "X-API-Key: $KEY" \
    "$BASE/api/machines/node-24/lease" | jq -r .readiness)" = returnable ]; do
  sleep 15
done
```

Expect roughly:

| Phase | Typical | Bound |
| --- | --- | --- |
| Runs finish (`polite`) | ~20 min | `max_run_minutes` |
| Runs killed (`eager`) | ~10 s | one worker tick |
| Production restored | 1–5 min | the model has to load |

The restore step is why `readiness` does not flip the moment the runs stop. The
deploy script returns in seconds; a 35 B model needs minutes to serve. `idle`
during that period means "our work is done, yours is not ready yet".

---

## Lifecycle

```
                 POST /machines/lease
   (no lease) ──────────────────────────▶  ACTIVE
                                             │
                      POST …/lease/end       │   due_at passes
                      {polite | eager}       │   (polite, automatic)
                                             ▼
                                         DRAINING
                                             │  runs finished or killed,
                                             │  production restored
                                             ▼
                                         RELEASED   readiness = returnable
```

`RELEASED` also sets the machine's `state` back to `away`, so nothing schedules
onto it until you lease it again.

---

## Guarantees

1. **Production is restored before `RELEASED`.** No mode skips it. If the
   restore does not match what was captured, the discrepancy is recorded as
   `baseline_restored_auto_with_drift` and is visible on the Resources page.
2. **`due_at` is honoured without you.** A lease that lapses drains politely by
   itself, so a caller that crashes does not strand a machine.
3. **A draining machine takes no new work**, from the moment the request lands.
4. **Nothing is torn down that was not first recorded.** Clearing requires a
   capture; a machine we could not inspect keeps its production service.

## What is *not* guaranteed

- **Instant hand-back.** Even `eager` needs a tick to kill runs and a few
  minutes to restore. If you need a machine in seconds, this API is the wrong
  tool — take it at the infrastructure layer and accept the wreckage.
- **That experiments completed.** Ending a lease early is expected to lose
  work; the campaign simply resumes on its next window if it has one.

---

## Errors

| Status | When | What to do |
| --- | --- | --- |
| `401` | Missing, malformed, or revoked key | Mint a new one. |
| `404` | No machine by that name | Lease it first (with `host`). |
| `409` on lease | Machine is mid-hand-back | Wait for `readiness: returnable`, then lease again. |
| `409` on end | No active lease | Already released; nothing to do. |
| `422` on lease | New machine without `host` | Supply `host`. |

---

## A full cycle

```bash
BASE=http://192.0.2.10:28100
KEY=atk_…

# lend it for the night
curl -sf -X POST "$BASE/api/machines/lease" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"node-24","host":"198.51.100.24","gpu_count":8,
       "due_at":"2026-08-05T00:00:00Z"}'

# ... the platform runs the night ...

# ask for it back, politely, but be off within 30 minutes
curl -sf -X POST "$BASE/api/machines/node-24/lease/end" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"mode":"polite","deadline_seconds":1800}'

# wait for production to be serving again
until [ "$(curl -sf -H "X-API-Key: $KEY" \
    "$BASE/api/machines/node-24/lease" | jq -r .readiness)" = returnable ]; do
  sleep 15
done
```

---

## Related

- [`/api/docs`](/api/docs) — every endpoint, with schemas you can call from the browser.
- **Campaigns** carry their own nightly window (`daily_start` / `daily_end`),
  which is independent of the lease: a campaign stands down at 08:00 and the
  machine goes back to production, then both resume at 23:00.
