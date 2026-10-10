# Documentation

Understanding it:

- [How it works](workflow.md): the product view, no code. 中文：[产品视角](workflow.zh.md)
- [Architecture](architecture.md): how it is built. 中文：[架构](architecture.zh.md)
- [Platform architecture](api/platform-architecture.md): the policy and a
  campaign, step by step. 中文：[平台架构](api/platform-architecture.zh.md)

Running it:

- [After installing](after-installing.md): settings, search policies, more
  GPUs, ingress and datasets.
- [Deploying](deploying.md): the Helm chart and every setting.
- [Upgrading](upgrading.md): how the schema is applied.

Building on it:

- [Policy API](api/policy-api.md): what a search container implements, in
  short. 中文：[policy API](api/policy-api.zh.md). The full contract:
  [policy-contract.md](api/policy-contract.md).
- [Machine lease API](api/machine-lease.md): lending GPU machines from
  another system, and taking them back.
- [Agent API](api/agent-api.md): what an LLM writes performance reports from
  (off by default).
  [Prompting the report agent](perf-report-prompt.md).
- [Plugins](plugins.md): routes, worker steps, tables, planners and pages from
  a separately installed package.
