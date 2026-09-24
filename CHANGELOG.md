# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-09-24

### Changed

- The `TuningRun` API group is now `tuning.modelsphere.dev` (was
  `tuning.llm-autotune.io`), and the operator labels its pods
  `tuning.modelsphere.dev/run`. An operator installed from 0.1.0 has to be
  reinstalled: a CRD cannot be renamed in place, and TuningRuns in the old group
  are not carried over (they are per-run and short-lived). Clusters already
  registered on the platform are moved to the new group by migration
  `002_crd_group`.
- The Deployments, Services and policy Jobs the platform creates itself are
  labelled `autotune.modelsphere.dev/{run,managed,policy}` (were
  `llm-autotune.io/…`). The platform finds its objects by these labels, so stop
  running campaigns before upgrading from 0.1.0; anything left over shows up
  with `kubectl get deploy,svc,job -l llm-autotune.io/managed=true`.

## [0.1.0] - 2026-09-24

### Added

- First public release of LLM AutoTune: campaigns, the launch stack (ssh+docker
  and Kubernetes drivers), search spaces and objectives, config baselines, the
  policy-as-code contract with an SDK and a reference policy, the promotion path,
  the agent API for generated performance reports, and a Helm chart that installs
  the whole platform.
- Images `4pdosc/llm-autotune-backend` and `4pdosc/llm-autotune-frontend` on
  Docker Hub, for amd64 and arm64. The mock engine, the operator and the
  policies are built from source.
- Released under the Apache License 2.0.

### Notes

- The policy SDK and the two reference policies live in
  [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies), checked out under `policies/` as a git
  submodule: clone with `--recurse-submodules`.

[0.1.1]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.1
[0.1.0]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.0
