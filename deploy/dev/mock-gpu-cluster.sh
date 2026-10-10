#!/usr/bin/env bash
# A kind cluster that looks like a GPU cluster, for trying the whole install
# on a laptop: the platform on one cluster, this one added from the UI with
# deploy/gpu-cluster.sh, and campaigns that run the mock engine on it.
#
#   deploy/dev/mock-gpu-cluster.sh            create it (or bring it up to date)
#   deploy/dev/mock-gpu-cluster.sh down       delete it
#
# What makes it pass for a GPU cluster:
#   - each node advertises --gpus nvidia.com/gpu (default 4), so engine pods
#     that ask for cards are scheduled, and the platform lists the nodes as GPU
#     nodes; no real card is behind them;
#   - an `nvidia` RuntimeClass that runs plain runc, the class engine pods ask for;
#   - the mock engine image (mock-engine/), loaded onto the nodes, and a
#     placeholder model directory at /var/lib/llm-autotune/mock-model.
# Nothing measured on it means anything about performance.
#
# Options:
#   --name NAME   the kind cluster's name (default: autotune-gpu; context kind-NAME)
#   --gpus N      cards each node advertises (default: 4)
set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
MODEL_PATH=/var/lib/llm-autotune/mock-model

action=up name=autotune-gpu gpus=4
while [ $# -gt 0 ]; do
  case "$1" in
    down) action=down ;;
    --name) [ $# -ge 2 ] || die "--name needs a value"; name=$2; shift ;;
    --gpus) [ $# -ge 2 ] || die "--gpus needs a value"; gpus=$2; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
case "$gpus" in ''|*[!0-9]*) die "--gpus must be a number" ;; esac
for tool in kind kubectl docker; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
ctx="kind-$name"

if [ "$action" = down ]; then
  kind delete cluster --name "$name"
  exit 0
fi

# kind hands this shell's proxy to the nodes, where a proxy on this machine's
# loopback (a local proxy app) is the node itself: every image pull fails.
proxy_env=()
for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
  case "${!var:-}" in
    *://127.*|*://localhost*|*://\[::1\]*)
      printf 'note: not passing %s=%s to the nodes; it is this machine'"'"'s loopback\n' "$var" "${!var}"
      proxy_env+=(-u "$var") ;;
  esac
done
if ! kind get clusters 2>/dev/null | grep -qx "$name"; then
  say "Creating kind cluster $name"
  env ${proxy_env[@]+"${proxy_env[@]}"} kind create cluster --name "$name" --wait 120s
fi
k() { kubectl --context "$ctx" "$@"; }

say "Advertising $gpus mock GPU(s) on each node"
for node in $(k get nodes -o jsonpath='{.items[*].metadata.name}'); do
  k patch node "$node" --subresource=status --type=json \
    -p "[{\"op\":\"add\",\"path\":\"/status/capacity/nvidia.com~1gpu\",\"value\":\"$gpus\"}]" >/dev/null
  k label node "$node" --overwrite nvidia.com/gpu.product=Mock-GPU >/dev/null
  docker exec "$node" sh -c "mkdir -p $MODEL_PATH && echo '{}' > $MODEL_PATH/config.json"
done

k apply -f - >/dev/null <<EOF
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: runc
EOF

version=$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$REPO/deploy/helm/llm-autotune/Chart.yaml")
image="llm-autotune-mock-engine:$version"
say "Building $image and loading it onto the nodes"
docker build -q -t "$image" "$REPO/mock-engine" >/dev/null
kind load docker-image "$image" --name "$name" >/dev/null 2>&1

cat <<EOF

Done: context $ctx, $(k get nodes --no-headers | wc -l | tr -d ' ') node(s) with $gpus mock GPU(s) each.

Add it to the platform as you would a real GPU cluster:
  deploy/gpu-cluster.sh --context $ctx --yes
then upload llm-autotune-runs.kubeconfig on Resources > Add GPU cluster.

In a campaign, use engine sglang, image $image and model path
$MODEL_PATH, and keep the benchmark small: the mock answers fast, but a
laptop runs the benchmark client too.
EOF
