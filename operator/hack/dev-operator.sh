#!/usr/bin/env bash
#
# Dev helper: build the autotune-operator and bring it up on the GPU cluster.
#
# Why it looks the way it does: the cluster and registry are only reachable from
# a bastion host (docker + a kubeconfig, but no Go), while this workstation has Go
# but cannot reach the cluster. So this script:
#   1. cross-compiles the manager locally (static, CGO off),
#   2. ships the binary to the bastion,
#   3. builds a tiny FROM-scratch image there and pushes it to the registry,
#   4. applies the kustomize install manifest through the bastion.
#
# Usage:
#   hack/dev-operator.sh build      # local only: compile + render install.yaml (no cluster)
#   hack/dev-operator.sh release    # build + push the image, pin install.yaml to its digest
#   hack/dev-operator.sh apply      # apply dist/install.yaml as-is (no build, no push)
#   hack/dev-operator.sh up         # build + push image + apply + wait for rollout
#   hack/dev-operator.sh down       # delete the operator (CRD + RBAC + manager)
#   hack/dev-operator.sh redeploy   # down then up
#   hack/dev-operator.sh status     # show the operator pod + CRD
#   hack/dev-operator.sh logs       # tail the manager logs
#
# Which host/kubeconfig to use is machine-specific: put it in a LOCAL, gitignored
# file (copy hack/dev-operator.env.example -> hack/dev-operator.env) or pass env
# vars, e.g.:
#   REMOTE=my-bastion REMOTE_KUBECONFIG=/path/to/kubeconfig hack/dev-operator.sh up
#
set -euo pipefail

# ---- config -----------------------------------------------------------------
# Machine-specific values (which bastion, which kubeconfig) come from a LOCAL,
# gitignored file so nothing about your cluster is committed. Env vars override.
ENV_FILE="$(dirname "${BASH_SOURCE[0]}")/dev-operator.env"
# shellcheck source=/dev/null
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

REMOTE="${REMOTE:-}"                                # ssh host that reaches the cluster API + the registry
REMOTE_KUBECONFIG="${REMOTE_KUBECONFIG:-}"          # path to the cluster kubeconfig ON that host
REMOTE_DIR="${REMOTE_DIR:-/tmp/autotune-operator}"  # staging dir on that host
IMG="${IMG:-ghcr.io/modelsphere/llm-autotune-operator:0.1.0}"  # image to build + push
NAMESPACE="${NAMESPACE:-autotune-operator-system}"
DEPLOY="deploy/autotune-operator-controller-manager"

# repo root = parent of this script's dir
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Fail with guidance when a var needed for cluster access is unset.
require() {
  local miss=0 v
  for v in "$@"; do
    [ -n "${!v:-}" ] || { printf '  missing: %s\n' "$v" >&2; miss=1; }
  done
  [ "$miss" -eq 0 ] || die "set the above in hack/dev-operator.env (copy the .env.example) or the environment"
}

# Find a Go toolchain (PATH, else the SDK we unpacked to ~/go-sdk), and put it on
# PATH so `make` (which calls plain `go`) finds the same one.
GO="${GO:-go}"
if ! command -v "$GO" >/dev/null 2>&1; then
  if [ -x "$HOME/go-sdk/go/bin/go" ]; then GO="$HOME/go-sdk/go/bin/go"
  else die "no 'go' on PATH and no ~/go-sdk/go/bin/go — set GO=/path/to/go"; fi
fi
export PATH="$(cd "$(dirname "$(command -v "$GO")")" && pwd):$PATH"

# Every remote step opens its OWN connection, on purpose.
#
# Do NOT add ControlMaster/ControlPath here. The bastion is reached through a
# long-lived ssh tunnel the operator runs by hand (`ssh -D … -N <host>`), and a
# shared control socket makes this script a client of THAT connection: when a
# deploy step ends, or a ControlPersist timer expires, it takes the operator's
# tunnel down with it. Multiplexing saves a handshake per step and costs the one
# connection everything else on this workstation depends on.
#
# The link is slow and occasionally drops. The answer is to retry a step, not to
# share a socket.
rsh() { ssh "$REMOTE" "$@"; }
rcp() { scp -qC "$@"; }

# kubectl on the bastion, against the cluster kubeconfig
kremote() { rsh "export KUBECONFIG=$REMOTE_KUBECONFIG; kubectl $*"; }

# ---- steps -----------------------------------------------------------------

local_build() {
  log "Cross-compiling manager (linux/amd64, static) — $("$GO" version | awk '{print $3}')"
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 "$GO" build -ldflags="-s -w" \
    -o bin/manager-linux-amd64 ./cmd/main.go

  log "Rendering install manifest for IMG=$IMG (regenerates CRD + RBAC)"
  make build-installer IMG="$IMG" >/dev/null
  echo "  -> dist/install.yaml"
}

push_image() {
  # The manager is ~50MB of static binary and the bastion is behind a ProxyJump,
  # so the raw scp this used to be was the slowest step of a deploy by far. gzip
  # it on the fly instead (~16MB) and inflate on arrival: nothing large is
  # written to disk twice, and no compressed copy is left behind on either end.
  #
  # Truncation can't slip through. gzip's trailer carries the uncompressed
  # length and a CRC32, so a stream cut short makes gunzip exit nonzero, the
  # `&&` chain stops, and the real filename is never moved into place — docker
  # build then fails on a missing file rather than baking half a binary into an
  # image. The chmod is not optional: a shell redirect creates the file under
  # the remote umask, and `COPY` carries the source mode into the image, so
  # without it the manager lands non-executable and the pod crash-loops.
  log "Shipping binary (gzip stream) + install.yaml to $REMOTE:$REMOTE_DIR"
  rsh "mkdir -p $REMOTE_DIR"
  gzip -c bin/manager-linux-amd64 | rsh "
    gunzip -c > $REMOTE_DIR/.manager.part &&
    chmod 0755 $REMOTE_DIR/.manager.part &&
    mv $REMOTE_DIR/.manager.part $REMOTE_DIR/manager-linux-amd64"
  rcp dist/install.yaml "$REMOTE:$REMOTE_DIR/"

  # A FROM-scratch image: the binary is static, so there is no base to pull and
  # the image is ~50MB. USER 65532 satisfies the manager Deployment's runAsNonRoot.
  rsh "cat > $REMOTE_DIR/Dockerfile" <<'DOCKERFILE'
FROM scratch
COPY manager-linux-amd64 /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
DOCKERFILE

  log "Building + pushing $IMG on $REMOTE"
  rsh "cd $REMOTE_DIR && docker build -t '$IMG' . && docker push '$IMG'"
}

cmd_build() { local_build; }

# Build + push the image and re-render dist/install.yaml PINNED TO THE DIGEST —
# and touch no cluster. This is what cutting a release is: the manifest someone
# else applies must name bytes, not a tag that can be rebuilt under them.
cmd_release() {
  require REMOTE
  local_build
  push_image
  local digest
  digest="$(rsh "docker inspect --format='{{index .RepoDigests 0}}' '$IMG'")"
  [ -n "$digest" ] || die "no digest for $IMG — did the push succeed?"
  log "Re-rendering dist/install.yaml at $digest"
  make build-installer IMG="$digest" >/dev/null
  grep -n "image: .*autotune-operator" dist/install.yaml
  echo
  echo "  -> dist/install.yaml is pinned to $digest; commit it."
}

cmd_up() {
  require REMOTE REMOTE_KUBECONFIG
  local_build
  push_image
  log "Applying install manifest (CRD + RBAC + manager)"
  kremote "apply -f $REMOTE_DIR/install.yaml"
  log "Waiting for rollout"
  kremote "-n $NAMESPACE rollout status $DEPLOY --timeout=120s"
  cmd_status
}

# Apply the release artifact AS COMMITTED — no compile, no push, no re-render.
# `up` is the dev loop (build what is in your tree, run it); this is what an
# admin does, and the only way to prove that the file we hand someone is the
# file that works.
cmd_apply() {
  require REMOTE REMOTE_KUBECONFIG
  [ -f dist/install.yaml ] || die "no dist/install.yaml — run '$0 release' first"
  log "Applying dist/install.yaml ($(grep -c '' dist/install.yaml) lines, image: $(grep -m1 'image: ' dist/install.yaml | awk '{print $2}'))"
  rsh "mkdir -p $REMOTE_DIR"
  rcp dist/install.yaml "$REMOTE:$REMOTE_DIR/"
  kremote "apply -f $REMOTE_DIR/install.yaml"
  log "Waiting for rollout"
  kremote "-n $NAMESPACE rollout status $DEPLOY --timeout=180s"
  cmd_status
}

cmd_down() {
  require REMOTE REMOTE_KUBECONFIG
  log "Deleting the operator"
  if rsh "test -f $REMOTE_DIR/install.yaml"; then
    kremote "delete -f $REMOTE_DIR/install.yaml --ignore-not-found"
  else
    # No staged manifest to delete by — fall back to deleting by name.
    kremote "delete ns $NAMESPACE --ignore-not-found"
    kremote "delete crd tuningruns.tuning.llm-autotune.io --ignore-not-found"
  fi
}

cmd_status() {
  require REMOTE REMOTE_KUBECONFIG
  log "Operator status"
  kremote "get ns $NAMESPACE" 2>/dev/null || echo "  (namespace $NAMESPACE not present)"
  kremote "get crd tuningruns.tuning.llm-autotune.io" 2>/dev/null || echo "  (CRD not installed)"
  kremote "-n $NAMESPACE get pods -o wide" 2>/dev/null || true
}

cmd_logs() { require REMOTE REMOTE_KUBECONFIG; kremote "-n $NAMESPACE logs -f $DEPLOY"; }

case "${1:-}" in
  build)    cmd_build ;;
  release)  cmd_release ;;
  apply)    cmd_apply ;;
  up)       cmd_up ;;
  down)     cmd_down ;;
  redeploy) cmd_down || true; cmd_up ;;
  status)   cmd_status ;;
  logs)     cmd_logs ;;
  *) echo "usage: $0 {build|release|apply|up|down|redeploy|status|logs}" >&2; exit 2 ;;
esac
