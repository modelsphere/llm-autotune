# Policies

A **policy** is a container that decides what to try next. The platform launches
it for a campaign's window, and it proposes configurations over the
[policy contract](../docs/api/policy-contract.md) — an HTTP API, so a policy can
be written in any language and versioned on its own.

The platform judges every proposal itself: it validates the configuration,
launches it, benchmarks it and scores it. A policy never reports its own result.
That split is what makes two policies comparable, and it is why adding a smarter
search requires no platform change at all.

| | |
|---|---|
| [`autotune_policy/`](autotune_policy/) | The SDK, and the copy of record — each policy repository vendors it verbatim, and CI here checks they still match. Stdlib only. `run_policy(SearchLoop(your_strategy))` satisfies every MUST in the contract — liveness, the productivity watchdog, idempotency — so you write search logic, not protocol. |
| `random-search/` → [autotune-policy-random-search](https://github.com/modelsphere/autotune-policy-random-search) | The reference policy, and the fork-and-edit template. Random search over the declared space: the baseline any smarter policy should beat. |
| `chaos/` → [autotune-policy-chaos](https://github.com/modelsphere/autotune-policy-chaos) | Not a tuner. It emits common policy failures on demand (crash, wedge, idle, bad config, container death) via `CHAOS_SCENARIO`, to prove the platform handles a policy gone wrong. |

Each policy is its own repository, checked out here as a git submodule, because
a policy is released on its own cadence and a fork of one should not have to
carry the platform with it:

```bash
git submodule update --init          # if you cloned without --recurse-submodules
```

## Writing one

Fork [autotune-policy-random-search](https://github.com/modelsphere/autotune-policy-random-search)
— it is the template, not just an example — and work in your own repository:

```bash
git clone https://github.com/<you>/autotune-policy-random-search my-policy
cd my-policy
# edit strategies/ and point main.py at your Strategy
uv run --extra dev pytest        # a full offline night, no platform needed
```

[The reference policy's README](https://github.com/modelsphere/autotune-policy-random-search#write-your-own-policy)
describes the five methods a strategy implements. [`BEST_PRACTICES.md`](BEST_PRACTICES.md) covers what the
platform enforces for you, so you can change the search logic without reasoning
about the protocol.

## Building an image

Each policy vendors the SDK, so its own directory is the build context and there
is nothing to install:

```bash
docker build -t my-policy:0.1.0 random-search/
```

Then register the image on the platform's Policies page and name it from a
campaign.
