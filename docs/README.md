# Documentation

Start here:

- [Architecture](architecture.md) — what the system is, how it is shaped, and the
  few choices that decide everything else. 中文：[架构概览](architecture.zh.md)
- [How it works](workflow.md) — the same system without the code, for anyone
  deciding whether it solves their problem. 中文：[产品视角](workflow.zh.md)

Running it:

- [Deploying](deploying.md) — the Helm chart, GPU access, the benchmark platform
- [Upgrading](upgrading.md) — how the schema is applied, and what is not
  upgradeable

The contracts other systems build against:

- [Platform architecture](api/platform-architecture.md) — the pieces and how a
  campaign flows through them. 中文：[平台架构](api/platform-architecture.zh.md)
- [Policy contract](api/policy-contract.md) — the full HTTP contract a search
  container implements. Narrative version: [policy API](api/policy-api.md)
  (中文：[policy API](api/policy-api.zh.md))
- [Machine lease API](api/machine-lease.md) — lending GPU machines to the
  platform from another system, and taking them back
- [Agent API](api/agent-api.md) — the read models an LLM writes performance
  reports from
