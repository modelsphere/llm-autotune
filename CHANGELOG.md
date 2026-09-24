# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.0
