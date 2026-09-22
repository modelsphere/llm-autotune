# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- First public release of LLM AutoTune: campaigns, the launch stack (ssh+docker
  and Kubernetes drivers), search spaces and objectives, config baselines, the
  policy-as-code contract with an SDK and a reference policy, the promotion path,
  the agent API for generated performance reports, and a Helm chart that installs
  the whole platform.
- Released under the Apache License 2.0.

### Notes

- The two reference policies live in their own repositories and are checked out
  under `policies/` as git submodules: clone with `--recurse-submodules`.
