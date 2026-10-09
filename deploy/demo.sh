#!/usr/bin/env bash
# A try-out with no GPUs: LLM AutoTune and LLMBench on a local kind cluster (or
# the cluster kubectl points at), and a demo campaign that tunes the mock
# engine, an image that answers sglang's endpoints with no GPU and no weights.
# Nothing it measures means anything about performance. For a deployment you
# keep, use deploy/quickstart.sh.
#
#   deploy/demo.sh --kind               create (or reuse) a kind cluster and install there
#   deploy/demo.sh                      install on the cluster kubectl points at
#   deploy/demo.sh ui                   port-forward both UIs
#   deploy/demo.sh policy DIR           build a search policy, load it into the cluster, register it
#   deploy/demo.sh down [--kind]        uninstall both (and delete the kind cluster)
#
# This machine needs kubectl, helm, docker, git and openssl, plus kind for
# --kind (give Docker about 8 GB of memory). Images built here load straight
# into kind, minikube, k3d, Docker Desktop or OrbStack; any other cluster pulls
# them from --registry.
#
# Options:
#   --kind            create (or reuse) a kind cluster named llm-autotune ($KIND_CLUSTER
#                     to name it otherwise), and use it
#   --context NAME    the kubectl context to install into (default: the current one)
#   --registry REPO   push images built here to REPO for the cluster to pull
#   --no-ui           finish without port-forwarding the UIs
#   --yes             do not ask before acting on a context that does not look local
#
# State lives in .demo/ (git-ignored).

# Read by lib/platforms.sh.
# shellcheck disable=SC2034
MODE=demo
MODE_SCRIPT=demo.sh
# shellcheck source=deploy/lib/platforms.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/platforms.sh"
