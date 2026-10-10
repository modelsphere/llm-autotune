#!/usr/bin/env bash
# LLM AutoTune and LLMBench, installed together on the Kubernetes cluster
# kubectl points at and wired by a shared service key: a deployment you keep.
#
#   deploy/quickstart.sh                install, or upgrade the same install
#   deploy/quickstart.sh ui             print both UIs' addresses (or port-forward them)
#   deploy/quickstart.sh policy DIR     build a search policy, deliver it to the cluster, register it
#   deploy/quickstart.sh down           uninstall both and delete their data
#
# The platforms need no GPUs. GPU clusters are added afterwards: run
# deploy/gpu-cluster.sh with each one's admin kubeconfig, then upload the
# kubeconfig it writes on the Resources page, which registers the cluster's GPU
# nodes (docs/after-installing.md). This machine needs kubectl, helm and
# openssl, plus git to fetch the LLMBench chart and docker to build policies.
#
# Options:
#   --registry REPO   push images built here (policies) to REPO for the cluster to pull
#   --context NAME    the kubectl context to install into (default: the current one)
#   --no-ui           finish without port-forwarding the UIs (CI)
#   --port-forward    reach the UIs through kubectl port-forward instead of a
#                     NodePort (the default only on a cluster on this machine)
#   --yes             do not ask before installing into a cluster that does not
#                     look local
#   policy DIR [--name NAME] [--env KEY=VALUE]... [--gpus] [--needs-model]
#                     NAME defaults to the directory's name; --env is passed to the
#                     policy container; --gpus / --needs-model for a policy that runs
#                     engines itself rather than delegating them to the platform
#
# Environment:
#   HF_ENDPOINT     a Hugging Face mirror, passed to LLMBench
#   LLMBENCH_CHART  a local llm-bench chart directory instead of the pinned release
#   AUTOTUNE_PORT, LLMBENCH_PORT   local ports for the two UIs when port-forwarded (8080, 8081)
#   NODE_HOST       the node address to print for the NodePorts (default: the first Ready node)
#
# State lives in .quickstart/ (git-ignored): the generated passwords and keys,
# so running it again upgrades the same install, and two values files of your
# own, llm-autotune.custom.yaml and llm-bench.custom.yaml, applied last.

# Read by lib/platforms.sh.
# shellcheck disable=SC2034
MODE=install
MODE_SCRIPT=quickstart.sh
# shellcheck source=deploy/lib/platforms.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/platforms.sh"
