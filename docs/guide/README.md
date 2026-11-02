# User guide

Using LLM AutoTune after installation: adding GPUs, defining what to tune,
running campaigns and reading the results. Installation is covered in
[Installing](../install.md).

![A finished campaign's leaderboard](images/campaign-leaderboard.png)

## Start here

1. **[Concepts](concepts.md)**: campaign, search space, objective, redline,
   policy, lease and the other terms used in the UI.
2. **[First campaign](first-campaign.md)**: from a fresh install to a tuned
   config, with a screenshot of each step. It also works on a local kind
   setup without GPUs.

## Pages

| page | covers | location in the UI |
|---|---|---|
| [Machines and GPU clusters](machines.md) | adding GPU clusters and ssh machines, leases, smoke tests, node groups | **Resources** |
| [Search spaces](search-spaces.md) | fixed, swept, ranged and tied parameters, conditions, YAML | **Library ▸ Search spaces** |
| [Objectives](objectives.md) | target metric, direction, redlines; choosing a metric | **Library ▸ Objectives** |
| [Datasets](datasets.md) | samples of production traffic for a Replay workload, and when they are refreshed | **Datasets** (top menu) |
| [Search policies](policies.md) | grid search and policies, `random-search`, registering a policy | **Library ▸ Policies** |
| [Creating a campaign](campaigns.md) | each step of New campaign; drafting from a baseline; YAML | **Campaigns ▸ New campaign** |
| [Running a campaign](running-campaigns.md) | statuses, run controls, schedules, pre-flight checks, the policy panel | a campaign's page |
| [Reading results](results.md) | leaderboard, run details, configuration, candidates, report, deploying the winner | a campaign's tabs |
| [Baselines](baselines.md) | recording production's config, comparing against it, Tune from this | **Library ▸ Baselines** |
| [Runs and failures](runs.md) | run states and failure kinds | **Runs** |
| [Account, users and API keys](account.md) | roles, adding users, passwords, the published name, API keys | the user menu |
| [Troubleshooting](troubleshooting.md) | symptoms and their usual causes | |

## How the parts relate

```text
                 Resources                        Library  →  Campaigns
         ┌─────────────────────┐   ┌──────────────────────────────────────────┐
         │ GPU clusters        │   │ Search space   Objective   Policy (opt.) │
         │   └─ machines ──────┼─┐ │       └────────────┼───────────┘         │
         │       (leased)      │ │ │                    ▼                     │
         └─────────────────────┘ └─┼──────────────►  Campaign  ◄── Baseline   │
                                   │                    │          (optional) │
                                   └────────────────────┼─────────────────────┘
                                                        ▼
                                   runs on the machines, measured by LLMBench
                                                        ▼
                                         leaderboard, report, winner
```

- A **campaign** copies a search space and an objective, runs on leased
  machines, and optionally uses a policy and a baseline.
- Each candidate config becomes a **run** on a machine; LLMBench measures it
  with the campaign's workload.
- Results appear on the campaign's **leaderboard**, ranked by the objective
  and, when a baseline exists, compared with production.

## Further reading

- [After installing](../after-installing.md): settings, more GPUs, ingress,
  replay datasets, building policies.
- [How it works](../workflow.md): the product view, with diagrams of a
  campaign and a tuning window.
- [Policy contract](../api/policy-contract.md) and
  [Machine lease API](../api/machine-lease.md): integrating with the platform.

The screenshots come from a local kind setup running the mock engine; their
numbers are not performance data.
