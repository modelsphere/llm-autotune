# shellcheck shell=bash
# The body of deploy/quickstart.sh and deploy/demo.sh: LLMBench and LLM
# AutoTune installed together on Kubernetes, wired by a shared service key.
# The entry script sets MODE and MODE_SCRIPT, then sources this file:
#   install  a deployment you keep, on the cluster kubectl points at
#   demo     a try-out with no GPUs: the mock engine and a demo campaign
set -euo pipefail

LLMBENCH_VERSION=0.1.2
KIND_CLUSTER=${KIND_CLUSTER:-llm-autotune}
AUTOTUNE_PORT=${AUTOTUNE_PORT:-8080}
LLMBENCH_PORT=${LLMBENCH_PORT:-8081}
BENCH_NS=llm-bench
TUNE_NS=llm-autotune
MOCK_MODEL_PATH=/var/lib/llm-autotune/mock-model

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="deploy/$MODE_SCRIPT"
if [ "$MODE" = demo ]; then STATE="$REPO/.demo"; else STATE="$REPO/.quickstart"; fi
CHART="$REPO/deploy/helm/llm-autotune"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required ($2)"; }

action=up ui=1 forward=0 yes=0 kind=0 context="" registry="" policy_dir="" policy_name="" gpus=0 needs_model=0
policy_env=""
while [ $# -gt 0 ]; do
  case "$1" in
    up|ui|down) action=$1 ;;
    policy)
      action=policy
      [ $# -ge 2 ] || die "policy needs a directory, e.g. $SCRIPT policy policies/random-search"
      policy_dir=$2; shift ;;
    --name) [ $# -ge 2 ] || die "--name needs a value"; policy_name=$2; shift ;;
    --env)
      case "${2:-}" in *=*) ;; *) die "--env needs KEY=VALUE" ;; esac
      value=${2#*=}; value=${value//\\/\\\\}; value=${value//\"/\\\"}
      policy_env="$policy_env\"${2%%=*}\": \"$value\", "; shift ;;
    --gpus) gpus=1 ;;
    --needs-model) needs_model=1 ;;
    --registry) [ $# -ge 2 ] || die "--registry needs a value"; registry=${2%/}; shift ;;
    --context) [ $# -ge 2 ] || die "--context needs a value"; context=$2; shift ;;
    --kind)
      [ "$MODE" = demo ] || die "--kind is for deploy/demo.sh: a kind cluster has no GPUs"
      kind=1 ;;
    --no-ui) ui=0 ;;
    --port-forward) forward=1 ;;
    --yes|-y) yes=1 ;;
    -h|--help) sed -n '2,/^$/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

# -- the cluster ---------------------------------------------------------------

need kubectl "https://kubernetes.io/docs/tasks/tools/"
need helm "https://helm.sh/docs/intro/install/"

if [ "$kind" = 1 ]; then
  need kind "https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
  if [ "$action" = up ] && ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
    say "Creating kind cluster $KIND_CLUSTER"
    kind create cluster --name "$KIND_CLUSTER"
  fi
  CTX="kind-$KIND_CLUSTER"
elif [ -n "$context" ]; then
  CTX=$context
elif [ -f "$STATE/context" ]; then
  CTX=$(cat "$STATE/context")  # every later command acts on the cluster it installed into
else
  CTX=$(kubectl config current-context 2>/dev/null) ||
    die "no current kubectl context: point kubectl at a cluster$([ "$MODE" = demo ] && echo ", or pass --kind to create one")"
fi
k() { kubectl --context "$CTX" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }

# A context is only a name: kubeadm calls every cluster's admin context
# "kubernetes-admin@kubernetes", so the same name can lead to another cluster
# after KUBECONFIG changes. The install remembers its API server as well, and
# refuses to act on a different one.
server_of_ctx() { k config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true; }
if [ "$kind" != 1 ] && [ -f "$STATE/server" ]; then
  current_server=$(server_of_ctx)
  [ "$current_server" = "$(cat "$STATE/server")" ] ||
    die "context $CTX now points at ${current_server:-nothing}, but this install lives on $(cat "$STATE/server"). Point KUBECONFIG at that cluster's kubeconfig$([ "$action" = up ] && echo ", or run $SCRIPT down there first to install elsewhere")."
fi

# The registry, once given, is remembered for later commands (policy).
if [ -n "$registry" ]; then
  mkdir -p "$STATE" && echo "$registry" > "$STATE/registry"
elif [ -f "$STATE/registry" ]; then
  registry=$(cat "$STATE/registry")
fi

# Installing into, or deleting from, a cluster that is not obviously a local
# one is asked about first.
confirm_context() {
  case "$CTX" in
    kind-*|k3d-*|minikube|docker-desktop|rancher-desktop|orbstack|colima*) return ;;
  esac
  [ "$yes" = 1 ] && return
  [ -t 0 ] || die "context $CTX does not look like a local cluster; pass --yes to $1 it"
  printf '%s LLMBench and LLM AutoTune in context "%s"? [y/N] ' "$1" "$CTX"
  read -r reply
  case "$reply" in y|Y|yes) ;; *) die "aborted" ;; esac
}

# Build an image from a directory here and make it available to the cluster's
# nodes. Prints the reference a pod should use. Engine and policy pods pull
# with IfNotPresent, so an image loaded onto the nodes is used as it is.
deliver_image() {  # <name:tag> <build context>
  local image=$1 context=$2 platform="" ref
  if [ -n "$registry" ]; then
    # The nodes may not share this machine's architecture.
    local arch
    arch=$(k get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}' 2>/dev/null || true)
    [ -n "$arch" ] && platform="--platform=linux/$arch"
  fi
  docker build -q ${platform:+"$platform"} -t "$image" "$context" >/dev/null
  if [ -n "$registry" ]; then
    ref="$registry/$image"
    docker tag "$image" "$ref"
    docker push -q "$ref" >/dev/null
    echo "$ref"
    return
  fi
  local out=""
  case "$CTX" in
    kind-*) out=$(kind load docker-image "$image" --name "${CTX#kind-}" 2>&1) ;;
    k3d-*) need k3d "to load images into k3d"; out=$(k3d image import "$image" -c "${CTX#k3d-}" 2>&1) ;;
    minikube) need minikube "to load images into minikube"; out=$(minikube image load "$image" 2>&1) ;;
    docker-desktop|orbstack) ;;  # the cluster runs on this Docker daemon and sees its images
    *) die "context $CTX cannot see images built on this machine; pass --registry REPO (one you can push to and its nodes can pull from)" ;;
  esac || die "could not load $image into $CTX: $out"
  echo "$image"
}

# The first lines of every script run against AutoTune's API. It runs inside
# the API's own pod: the same calls the UI makes, with no Python or
# port-forward needed on this machine.
api_script() {  # <extra config as Python dict items>
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  printf 'CFG = {"password": "%s", %s}\n' "$AUTOTUNE_ADMIN_PASSWORD" "$1"
  cat <<'PY'
import json, sys, time, urllib.error, urllib.request

API = "http://127.0.0.1:8000/api"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:300]
    except OSError as exc:
        return 0, str(exc)


def until(what, attempt, minutes=10):
    """Retry while the migrate job (schema, first admin, built-ins) catches up."""
    deadline = time.monotonic() + minutes * 60
    while True:
        result = attempt()
        if result:
            return result
        if time.monotonic() > deadline:
            sys.exit(f"gave up waiting for {what}")
        time.sleep(5)


def login():
    status, body = call("POST", "/auth/login", {"username": "admin", "password": CFG["password"]})
    return body["access_token"] if status == 200 else None


token = until("the admin account", login)
PY
}
run_api() { k -n "$TUNE_NS" exec -i deploy/llm-autotune-api -- python -; }

# -- down ----------------------------------------------------------------------

if [ "$action" = down ]; then
  confirm_context "Delete"
  say "Removing both platforms from $CTX"
  h uninstall llm-autotune -n "$TUNE_NS" --wait 2>/dev/null || true
  h uninstall llm-bench -n "$BENCH_NS" --wait 2>/dev/null || true
  # LLMBench's worker drains a running benchmark for hours before it exits, and
  # everything here is being deleted anyway: do not wait for it.
  for ns in "$TUNE_NS" "$BENCH_NS"; do
    k -n "$ns" delete pods --all --grace-period=0 --force >/dev/null 2>&1 || true
  done
  # The namespaces take the databases' volumes with them: a new install then
  # starts clean, with new secrets. Your *.custom.yaml files are kept.
  k delete namespace "$TUNE_NS" "$BENCH_NS" --ignore-not-found --wait
  rm -f "$STATE/secrets.env" "$STATE/context" "$STATE/server" "$STATE/ui.env" "$STATE"/*.values.yaml
  if [ "$kind" = 1 ] && kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
    kind delete cluster --name "$KIND_CLUSTER"
  fi
  echo "Done."
  exit 0
fi

[ -f "$STATE/secrets.env" ] || [ "$action" = up ] ||
  die "nothing installed from here yet; run $SCRIPT first"

# -- ui ------------------------------------------------------------------------

# How a person reaches the two UIs. An ingress of your own (publicUiUrl in the
# custom values) is used as it is. A cluster on this machine (kind, Docker
# Desktop, ...) is reached by port-forward, as is any cluster with
# --port-forward. Any other cluster opens both UIs on a NodePort: every node
# answers on it, so the UIs are reachable without a tunnel from wherever the
# nodes are. The address and ports are kept in ui.env, so later runs reuse them.
ui_access() {
  if [ "$forward" = 1 ]; then echo port-forward
  elif grep -qs '^publicUiUrl:' "$STATE/llm-autotune.custom.yaml"; then echo ingress
  else
    case "$CTX" in
      kind-*|k3d-*|minikube|docker-desktop|rancher-desktop|orbstack|colima*) echo port-forward ;;
      *) echo nodeport ;;
    esac
  fi
}
ACCESS=$(ui_access)
node_host_env=${NODE_HOST:-}
NODE_HOST="" AUTOTUNE_NODEPORT="" LLMBENCH_NODEPORT=""
# shellcheck disable=SC1091
[ ! -f "$STATE/ui.env" ] || . "$STATE/ui.env"
[ -z "$node_host_env" ] || NODE_HOST=$node_host_env

# The address of the first Ready node; NODE_HOST names another.
pick_node_host() {
  k get nodes -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end} {range .status.addresses[?(@.type=="InternalIP")]}{.address}{end}{"\n"}{end}' |
    awk '$1 == "True" && $2 != "" {print $2; exit}'
}

# AUTOTUNE_URL and LLMBENCH_URL: where a browser opens each UI. Empty while a
# NodePort is not assigned yet. PUBLIC_API_URL: where a policy container on a
# GPU cluster calls the platform back, the UI's own address (its nginx serves
# /api); empty when that is only localhost, which no GPU cluster can reach.
ui_urls() {
  AUTOTUNE_URL="" LLMBENCH_URL="" PUBLIC_API_URL=""
  case "$ACCESS" in
    port-forward)
      AUTOTUNE_URL="http://localhost:$AUTOTUNE_PORT" LLMBENCH_URL="http://localhost:$LLMBENCH_PORT" ;;
    nodeport)
      if [ -n "$NODE_HOST" ] && [ -n "$AUTOTUNE_NODEPORT" ]; then AUTOTUNE_URL="http://$NODE_HOST:$AUTOTUNE_NODEPORT"; fi
      if [ -n "$NODE_HOST" ] && [ -n "$LLMBENCH_NODEPORT" ]; then LLMBENCH_URL="http://$NODE_HOST:$LLMBENCH_NODEPORT"; fi
      PUBLIC_API_URL=$AUTOTUNE_URL ;;
    ingress)
      AUTOTUNE_URL=$(sed -n 's/^publicUiUrl: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$STATE/llm-autotune.custom.yaml")
      PUBLIC_API_URL=$AUTOTUNE_URL
      LLMBENCH_URL=$(sed -n 's/^ *webUrl: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$STATE/llm-autotune.custom.yaml" | head -1) ;;
  esac
}

open_ui() {
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  ui_urls
  cat <<EOF

  LLM AutoTune   ${AUTOTUNE_URL:-(see your ingress)}   admin / $AUTOTUNE_ADMIN_PASSWORD
  LLMBench       ${LLMBENCH_URL:-(see your ingress)}   $LLMBENCH_ADMIN_EMAIL / $LLMBENCH_ADMIN_PASSWORD
EOF
  case "$ACCESS" in
    nodeport)
      cat <<EOF

Both UIs are open on these ports on every node; $NODE_HOST is one of them
(NODE_HOST=<address> $SCRIPT names another). --port-forward reaches them through
kubectl instead.
EOF
      return ;;
    ingress) return ;;
  esac
  printf '\nPort-forwarding both UIs; Ctrl-C stops it (%s ui starts it again).\n' "$SCRIPT"
  k -n "$TUNE_NS" port-forward svc/llm-autotune-frontend "$AUTOTUNE_PORT:80" >/dev/null &
  k -n "$BENCH_NS" port-forward svc/llm-bench-frontend "$LLMBENCH_PORT:80" >/dev/null &
  trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
  wait
}

if [ "$action" = ui ]; then
  open_ui
  exit 0
fi

# -- policy --------------------------------------------------------------------

if [ "$action" = policy ]; then
  need docker "to build the policy image"
  # Policy pods run on the GPU clusters, not on this one: only the demo, whose
  # mock runs live here, can load the image into the cluster directly.
  [ "$MODE" = demo ] || [ -n "$registry" ] ||
    die "pass --registry REPO: a registry you can push to and your GPU clusters' nodes pull from"
  dir=$(cd "$policy_dir" 2>/dev/null && pwd) || die "no such directory: $policy_dir"
  if [ ! -f "$dir/Dockerfile" ] && [ "${dir#"$REPO/policies"}" != "$dir" ]; then
    say "Fetching the policies submodule"
    git -C "$REPO" submodule update --init policies
  fi
  [ -f "$dir/Dockerfile" ] || die "$dir has no Dockerfile; a policy directory builds with docker build"
  name=${policy_name:-$(basename "$dir")}
  # A new tag per build: nodes keep images they already have, so reusing a tag
  # would keep running the old build.
  tag=$(date -u +%Y%m%d%H%M%S)
  say "Building policy $name"
  ref=$(deliver_image "llm-autotune-policy-$name:$tag" "$dir")
  say "Registering $ref"
  {
    api_script "\"name\": \"$name\", \"image\": \"$ref\", \"version\": \"$tag\", \"env\": {${policy_env%, }}, \"gpus\": $([ "$gpus" = 1 ] && echo True || echo False), \"needs_model\": $([ "$needs_model" = 1 ] && echo True || echo False)"
    cat <<'PY'
body = {"name": CFG["name"], "image": CFG["image"], "version": CFG["version"], "env": CFG["env"],
        "gpus_in_container": CFG["gpus"], "needs_model": CFG["needs_model"]}
status, rows = call("GET", "/policies", token=token)
existing = next((p for p in rows if p["name"] == CFG["name"]), None) if status == 200 else None
if existing is None:
    status, out = call("POST", "/policies", body, token)
else:
    status, out = call("PUT", f"/policies/{existing['id']}", {**existing, **body}, token)
if status not in (200, 201):
    sys.exit(f"could not register the policy: {status} {out}")
print(f"   policy \"{CFG['name']}\" {'registered' if existing is None else 'updated'}: {CFG['image']}")
PY
  } | run_api
  echo "In New campaign, pick it under Search > Strategy."
  exit 0
fi

# -- up ------------------------------------------------------------------------

if [ "$MODE" = demo ]; then need docker "to build the mock engine image"; fi
need openssl "to generate passwords and keys"
[ -n "${LLMBENCH_CHART:-}" ] || need git "to fetch the LLMBench chart"

k get namespace default --request-timeout=15s >/dev/null ||
  die "cannot reach the cluster of context $CTX$([ -f "$STATE/context" ] && echo " (where this install lives; to install somewhere else, run $SCRIPT down first)")"

# Before 0.2 the quickstart installed the demo; that install is the demo's now.
if [ "$MODE" = install ] && grep -qs '^mockModel:' "$STATE/llm-autotune.values.yaml"; then
  die "$STATE holds a demo install, made by the quickstart before 0.2. It belongs
to deploy/demo.sh now: run  mv .quickstart .demo  and use deploy/demo.sh (with
--kind if it is on kind); deploy/demo.sh down removes it."
fi

# Never take over a release this script did not install (the other script's,
# or one installed by hand).
if [ ! -f "$STATE/secrets.env" ] && h status llm-autotune -n "$TUNE_NS" >/dev/null 2>&1; then
  die "context $CTX already has an llm-autotune release in namespace $TUNE_NS that $SCRIPT did not install"
fi

confirm_context "Install"

# The platform itself needs no GPUs: GPU clusters are added afterwards, from
# the Resources page, with a kubeconfig deploy/gpu-cluster.sh writes for each.
# Only the demo runs its mock engine on this same cluster (values-demo.yaml).

mkdir -p "$STATE"
chmod 700 "$STATE"
echo "$CTX" > "$STATE/context"
[ "$kind" = 1 ] || server_of_ctx > "$STATE/server"

# Secrets, once. URL-safe alphabet only, so they need no quoting anywhere.
token() { openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '=\n'; }
if [ ! -f "$STATE/secrets.env" ]; then
  say "Generating passwords and keys ($STATE/secrets.env)"
  umask 077
  cat > "$STATE/secrets.env" <<EOF
LLMBENCH_SECRET_KEY=$(token 32)
LLMBENCH_PLATFORM_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
LLMBENCH_ADMIN_EMAIL=admin@example.com
LLMBENCH_ADMIN_PASSWORD=$(token 15)
LLMBENCH_POSTGRES_PASSWORD=$(token 24)
SERVICE_API_KEY=llmb_$(token 32)
AUTOTUNE_JWT_SECRET=$(openssl rand -hex 32)
AUTOTUNE_ADMIN_PASSWORD=$(token 15)
AUTOTUNE_POSTGRES_PASSWORD=$(token 24)
EOF
fi
# shellcheck disable=SC1091
. "$STATE/secrets.env"

# Values of your own, applied after everything this script sets. Created once,
# never overwritten, kept by `down`.
if [ ! -f "$STATE/llm-autotune.custom.yaml" ]; then
  {
    cat <<EOF
# Your values for the LLM AutoTune release, applied last on every run of
# $SCRIPT: edit, then run it again. Every value is in
# deploy/helm/llm-autotune/values.yaml; docs/after-installing.md walks through
# the common ones. For example:
#
EOF
    if [ "$MODE" = install ]; then
      cat <<'VALUES'
# Keep runs on the nodes that hold the model weights:
# gpuCluster:
#   nodeSelector: "nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3"
#
# Engine logs in the UI, given a ReadWriteMany storage class:
# runLogs:
#   enabled: true
#   storageClass: nfs-client
#
# The UI on an ingress instead of port-forward:
# ingress:
#   enabled: true
#   className: nginx
#   host: autotune.example.com
# publicUiUrl: https://autotune.example.com
VALUES
    else
      cat <<'VALUES'
# Any platform setting (docs/deploying.md#other-settings):
# extraEnv:
#   - name: AUTOTUNE_DEFAULT_DAILY_START
#     value: "22:00"
VALUES
    fi
  } > "$STATE/llm-autotune.custom.yaml"
fi
if [ ! -f "$STATE/llm-bench.custom.yaml" ]; then
  cat > "$STATE/llm-bench.custom.yaml" <<'EOF'
# Your values for the LLMBench release, applied last on every run of
# the script that installed it: edit, then run it again. Every value is in LLMBench's
# deploy/helm/llm-bench/values.yaml. For example, a datasets volume for the
# academic suites and replay captures:
#
# datasets:
#   enabled: true
#   storageClass: ""              # needs ReadOnlyMany/ReadWriteMany, or pin the workers
#   accessMode: ReadWriteOnce
EOF
fi

# The LLMBench chart, from the release this version of AutoTune was tested with.
if [ -n "${LLMBENCH_CHART:-}" ]; then
  bench_chart=$LLMBENCH_CHART
else
  src="$STATE/llm-bench-$LLMBENCH_VERSION"
  if [ ! -d "$src" ]; then
    say "Fetching the LLMBench $LLMBENCH_VERSION chart"
    # Fetched by tag rather than cloned: a shallow clone of an annotated tag
    # warns, and a checkout of one prints git's detached-HEAD advice.
    rm -rf "$src.partial"
    git init -q "$src.partial"
    # Some proxies break git's HTTP/2 mid-response ("Error in the HTTP2
    # framing layer"); HTTP/1.1 gets through them.
    git -C "$src.partial" fetch -q --depth 1 https://github.com/modelsphere/llm-bench tag "v$LLMBENCH_VERSION" ||
      git -C "$src.partial" -c http.version=HTTP/1.1 fetch -q --depth 1 https://github.com/modelsphere/llm-bench tag "v$LLMBENCH_VERSION" ||
      die "could not fetch the LLMBench $LLMBENCH_VERSION chart from GitHub; with a clone of llm-bench at hand, set LLMBENCH_CHART=<clone>/deploy/helm/llm-bench"
    git -C "$src.partial" -c advice.detachedHead=false checkout -q "v$LLMBENCH_VERSION"
    mv "$src.partial" "$src"
  fi
  bench_chart="$src/deploy/helm/llm-bench"
fi

app_version=$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$CHART/Chart.yaml")
mock_image=""
if [ "$MODE" = demo ]; then
  say "Building the mock engine and loading it into the cluster"
  mock_image=$(deliver_image "llm-autotune-mock-engine:$app_version" "$REPO/mock-engine")
fi

# Values files hold the secrets, so they stay out of the process list. Written
# again once the UIs' NodePorts are known, so each UI's links point at the other.
write_values() {
  ui_urls
  umask 077
  {
    cat <<EOF
secrets:
  secretKey: "$LLMBENCH_SECRET_KEY"
  platformSecretKey: "$LLMBENCH_PLATFORM_SECRET_KEY"
  adminEmail: "$LLMBENCH_ADMIN_EMAIL"
  adminUsername: admin
  adminPassword: "$LLMBENCH_ADMIN_PASSWORD"
  serviceUsername: autotune
  serviceApiKey: "$SERVICE_API_KEY"
postgres:
  password: "$LLMBENCH_POSTGRES_PASSWORD"
EOF
    if [ -n "${HF_ENDPOINT:-}" ]; then
      printf 'app:\n  extraEnv:\n    - name: HF_ENDPOINT\n      value: "%s"\n' "$HF_ENDPOINT"
    fi
    if [ "$ACCESS" = nodeport ]; then
      printf 'service:\n  frontend:\n    type: NodePort\n'
      [ -z "$LLMBENCH_NODEPORT" ] || printf '    nodePort: %s\n' "$LLMBENCH_NODEPORT"
    fi
  } > "$STATE/llm-bench.values.yaml"
  {
    cat <<EOF
jwtSecret: "$AUTOTUNE_JWT_SECRET"
adminPassword: "$AUTOTUNE_ADMIN_PASSWORD"
publicUiUrl: "$AUTOTUNE_URL"
publicApiUrl: "$PUBLIC_API_URL"
postgresql:
  password: "$AUTOTUNE_POSTGRES_PASSWORD"
llmbench:
  url: http://llm-bench-backend.$BENCH_NS:8000
  webUrl: "$LLMBENCH_URL"
  apiKey: "$SERVICE_API_KEY"
EOF
    if [ "$MODE" = demo ]; then
      printf 'mockModel:\n  image: "%s"\n' "$mock_image"
    else
      # Engine logs need a ReadWriteMany volume, which a cluster may not have:
      # off until the custom values say.
      printf 'runLogs:\n  enabled: false\n'
    fi
    if [ "$ACCESS" = nodeport ]; then
      printf 'frontend:\n  service:\n    type: NodePort\n'
      [ -z "$AUTOTUNE_NODEPORT" ] || printf '    nodePort: %s\n' "$AUTOTUNE_NODEPORT"
    fi
  } > "$STATE/llm-autotune.values.yaml"
}
write_values

# First pulls of the images can take a while on a slow connection; nothing
# here fails before this budget runs out.
WAIT=20m

# A run stopped mid-install (Ctrl-C, a lost shell) leaves its release failed or
# pending, and helm then refuses or half-redoes the next install. A release
# that never finished a first install holds nothing worth keeping: remove it
# and start that one over. One stopped mid-upgrade goes back to its last
# working revision first.
recover_release() {  # <release> <namespace>
  local status
  status=$(h status "$1" -n "$2" 2>/dev/null | sed -n 's/^STATUS: //p') || true
  case "$status" in
    failed|pending-install|pending-upgrade|pending-rollback) ;;
    *) return 0 ;;
  esac
  if ! h history "$1" -n "$2" -o json 2>/dev/null | grep -qE '"status":"(deployed|superseded)"'; then
    say "An earlier install of $1 did not finish ($status); removing it to start over"
    h uninstall "$1" -n "$2" --wait >/dev/null
    # Its migrate job is a hook, which helm does not remove with the release.
    k -n "$2" delete jobs --all --ignore-not-found >/dev/null 2>&1 || true
  elif [ "$status" != failed ]; then
    say "An earlier upgrade of $1 did not finish ($status); rolling back to its last working revision"
    h rollback "$1" -n "$2" --wait >/dev/null
  fi
}

# Pods in <namespace> that are stuck rather than starting, one line each: an
# image that will not pull, a container that keeps crashing (with the end of
# its log), a pod nothing can schedule.
stuck_pods() {  # <namespace>
  local name reasons message
  k -n "$1" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{range .status.initContainerStatuses[*]}{.state.waiting.reason}{" "}{end}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{end}{"|"}{range .status.conditions[?(@.type=="PodScheduled")]}{.status}{":"}{.message}{end}{"\n"}{end}' 2>/dev/null |
    # "|", not a tab: read collapses empty tab-separated fields, and a pod
    # with nothing waiting has an empty middle one.
    while IFS='|' read -r name reasons message; do
      case "$reasons" in
        *ImagePullBackOff*|*ErrImagePull*|*InvalidImageName*|*CreateContainerConfigError*)
          printf '   %s: %s %s\n' "$name" "$(echo "$reasons" | xargs)" \
            "$(k -n "$1" get pod "$name" -o jsonpath='{.spec.initContainers[*].image} {.spec.containers[*].image}' 2>/dev/null)" ;;
        *CrashLoopBackOff*|*RunContainerError*)
          printf '   %s: %s\n' "$name" "$(echo "$reasons" | xargs)"
          k -n "$1" logs "$name" --all-containers --tail=3 2>/dev/null | sed 's/^/       | /' || true ;;
        *)
          case "$message" in
            False:?*) printf '   %s: not scheduled: %s\n' "$name" "${message#False:}" ;;
          esac ;;
      esac
    done
}

# Run a command that waits on <namespace>, saying what is stuck while it does:
# helm and kubectl wait silently, so a crash-looping pod looked like a hang.
watching() {  # <namespace> <command...>
  local ns=$1 pid seen="" now log
  shift
  log=$(mktemp)
  "$@" >"$log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 10
    kill -0 "$pid" 2>/dev/null || break
    now=$(stuck_pods "$ns")
    if [ -n "$now" ] && [ "$now" != "$seen" ]; then
      printf '   still waiting; stuck in %s:\n%s\n' "$ns" "$now"
    fi
    seen=$now
  done
  if ! wait "$pid"; then
    cat "$log" >&2
    rm -f "$log"
    now=$(stuck_pods "$ns")
    [ -z "$now" ] || printf 'stuck in %s:\n%s\n' "$ns" "$now" >&2
    die "waiting on $ns failed (above); kubectl --context $CTX -n $ns get pods"
  fi
  # On success the output is helm's release notes and kubectl's progress
  # lines: the script says where the UIs are itself, at the end.
  rm -f "$log"
}

say "Installing LLMBench into namespace $BENCH_NS"
recover_release llm-bench "$BENCH_NS"
watching "$BENCH_NS" h upgrade --install llm-bench "$bench_chart" -n "$BENCH_NS" --create-namespace \
  --timeout "$WAIT" -f "$STATE/llm-bench.values.yaml" -f "$STATE/llm-bench.custom.yaml"
for d in postgres redis backend worker frontend; do
  watching "$BENCH_NS" k -n "$BENCH_NS" rollout status "deploy/llm-bench-$d" --timeout="$WAIT"
done

say "Waiting for LLMBench to accept AutoTune's service key"
# The key only works once LLMBench's migrate job has seeded the service account.
bench_ready=0
for _ in $(seq 1 120); do
  if printf 'import urllib.request\nr = urllib.request.Request("http://127.0.0.1:8000/auth/me", headers={"Authorization": "Bearer %s"})\nurllib.request.urlopen(r, timeout=5)\n' \
      "$SERVICE_API_KEY" | k -n "$BENCH_NS" exec -i deploy/llm-bench-backend -- python - >/dev/null 2>&1; then
    bench_ready=1
    break
  fi
  sleep 5
done
[ "$bench_ready" = 1 ] || die "LLMBench did not accept the service key; see: kubectl --context $CTX -n $BENCH_NS logs job/llm-bench-migrate"

say "Installing LLM AutoTune into namespace $TUNE_NS"
demo_values=()
if [ "$MODE" = demo ]; then demo_values=(-f "$CHART/values-demo.yaml"); fi
install_autotune() {
  watching "$TUNE_NS" h upgrade --install llm-autotune "$CHART" -n "$TUNE_NS" --create-namespace \
    --timeout "$WAIT" ${demo_values[@]+"${demo_values[@]}"} -f "$STATE/llm-autotune.values.yaml" \
    -f "$STATE/llm-autotune.custom.yaml"
  watching "$TUNE_NS" k -n "$TUNE_NS" rollout status statefulset/llm-autotune-postgresql --timeout="$WAIT"
  for d in api worker frontend; do
    watching "$TUNE_NS" k -n "$TUNE_NS" rollout status "deploy/llm-autotune-$d" --timeout="$WAIT"
  done
}
recover_release llm-autotune "$TUNE_NS"
install_autotune

# With NodePorts, the ports are only known now. Keep them (so later runs ask
# for the same ones) and point each UI's links at the other's address.
if [ "$ACCESS" = nodeport ]; then
  [ -n "$NODE_HOST" ] || NODE_HOST=$(pick_node_host)
  [ -n "$NODE_HOST" ] || die "no Ready node with an InternalIP to reach the UIs on; set NODE_HOST=<address>"
  old_urls="$AUTOTUNE_NODEPORT $LLMBENCH_NODEPORT"
  AUTOTUNE_NODEPORT=$(k -n "$TUNE_NS" get svc llm-autotune-frontend -o jsonpath='{.spec.ports[0].nodePort}')
  LLMBENCH_NODEPORT=$(k -n "$BENCH_NS" get svc llm-bench-frontend -o jsonpath='{.spec.ports[0].nodePort}')
  printf 'NODE_HOST=%s\nAUTOTUNE_NODEPORT=%s\nLLMBENCH_NODEPORT=%s\n' \
    "$NODE_HOST" "$AUTOTUNE_NODEPORT" "$LLMBENCH_NODEPORT" > "$STATE/ui.env"
  if [ "$old_urls" != "$AUTOTUNE_NODEPORT $LLMBENCH_NODEPORT" ] ||
      ! grep -q "publicUiUrl: \"http://$NODE_HOST:$AUTOTUNE_NODEPORT\"" "$STATE/llm-autotune.values.yaml"; then
    # The ports exist only once the services do: a second, short upgrade
    # gives each UI the other's address for its links.
    say "Pointing each UI's links at the other (http://$NODE_HOST, ports $AUTOTUNE_NODEPORT and $LLMBENCH_NODEPORT)"
    write_values
    install_autotune
  fi
fi
if k -n "$TUNE_NS" get ds/llm-autotune-mock-model >/dev/null 2>&1; then
  watching "$TUNE_NS" k -n "$TUNE_NS" rollout status ds/llm-autotune-mock-model --timeout="$WAIT"
fi

if [ "$MODE" = demo ]; then say "Wiring the two and starting the demo campaign"; else say "Wiring the two"; fi
{
  api_script "\"demo\": $([ "$MODE" = demo ] && echo True || echo False), \"image\": \"$mock_image\", \"model_path\": \"$MOCK_MODEL_PATH\""
  cat <<'PY'


def ensure():
    status, body = call("POST", "/benchmarks/ensure", {}, token)
    if status == 200:
        return body
    print(f"   LLMBench not ready for AutoTune yet ({status}: {body}); retrying")
    return None


ensured = until("the screen benchmark on LLMBench", ensure)
print(f"   AutoTune measures runs with {ensured['slug']} on LLMBench "
      f"({'created' if ensured['created'] else 'present'}, locked)")
if not CFG["demo"]:
    sys.exit(0)

# The demo screens with a lighter benchmark: on a laptop the default one
# measures the benchmark client rather than the mock (see the template).
DEMO_BENCHMARK = "autotune-quickstart-v1"
status, body = call("POST", "/benchmarks/ensure", {"template": DEMO_BENCHMARK}, token)
if status != 200:
    sys.exit(f"could not create {DEMO_BENCHMARK} on LLMBench: {status} {body}")


def machine():
    status, rows = call("GET", "/machines", token=token)
    return next((m for m in rows if m["name"] == "local-cluster"), None) if status == 200 else None


m = until("the local-cluster machine", machine)
if m.get("lease_state") != "active":
    fields = ("name", "host", "ssh_user", "ssh_port", "gpu_count", "gpu_type")
    status, body = call("POST", "/machines/lease",
                        {**{k: m.get(k) for k in fields}, "lease_note": "leased by deploy/demo.sh"},
                        token)
    if status != 200:
        sys.exit(f"could not lease local-cluster: {status} {body}")
print("   local-cluster is leased to the platform")

SPACE = "Quickstart: mock token speed"
status, spaces = call("GET", "/search-spaces", token=token)
space = next((s for s in spaces if s["name"] == SPACE), None)
if space is None:
    status, space = call("POST", "/search-spaces", {
        "name": SPACE, "engine": "sglang",
        "description": "Two speeds of the mock engine; the faster one should rank first.",
        "base": {}, "grid": {"mock_token_ms": [2, 20]},
    }, token)
    if status != 200:
        sys.exit(f"could not create the search space: {status} {space}")


def objective():
    status, rows = call("GET", "/objectives", token=token)
    return next((o for o in rows if o["is_builtin"] and o["name"] == "Throughput per GPU"), None) \
        if status == 200 else None


obj = until("the built-in objectives", objective)

NAME = "Quickstart: mock engine"
status, campaigns = call("GET", "/campaigns", token=token)
campaign = next((c for c in campaigns if c["name"] == NAME), None)
if campaign is None:
    status, campaign = call("POST", "/campaigns", {
        "name": NAME, "engine": "sglang", "image": CFG["image"], "model_path": CFG["model_path"],
        "served_model_name": "mock", "extra_env": {}, "extra_volumes": {},
        "machine_names": ["local-cluster"], "node_group": "", "service_port": 28200,
        "share_machine": True, "max_run_minutes": 30, "benchmark_slug": DEMO_BENCHMARK,
        "policy_id": None, "policy_settings": {}, "confirm_top_k": 0, "confirm_repeats": 3,
        "run_baseline_canary": False, "daily_start": "23:00", "daily_end": "08:00",
        "schedule_timezone": "UTC", "schedule_until": None,
        "search_space": {k: space[k] for k in ("name", "base", "grid", "tied", "range", "conditions")},
        "objective": {k: obj[k] for k in ("name", "target_metric", "direction", "redlines")},
        "verify_benchmark_slug": "", "verify_top_k": 0, "verify_max_run_minutes": 180,
        "verify_objective": {}, "dataset_profile": "", "dataset_policy": "rebuild_at_start",
    }, token)
    if status != 200:
        sys.exit(f"could not create the demo campaign: {status} {campaign}")
    # Campaigns run in a nightly window; the demo starts now.
    status, body = call("POST", f"/campaigns/{campaign['id']}/force-start", {}, token)
    if status != 200:
        sys.exit(f"could not start the demo campaign: {status} {body}")
    print(f"   started campaign {campaign['id']} \"{NAME}\": two configurations, about a minute each")
else:
    print(f"   campaign {campaign['id']} \"{NAME}\" already exists ({campaign['status']})")
PY
} | run_api

say "Both platforms are up"
if [ "$MODE" = demo ]; then
  cat <<EOF
In LLM AutoTune, open Campaigns > "Quickstart: mock engine": each run goes
pending > launching > health_check > benching > succeeded, and the leaderboard
ranks the faster configuration first. Each run is also a submission on LLMBench.
EOF
else
  cat <<EOF
Next, give the platform GPUs. For each GPU cluster, with that cluster's admin
kubeconfig:

  deploy/gpu-cluster.sh            # writes llm-autotune-runs.kubeconfig

then in LLM AutoTune open Resources > Add GPU cluster, upload the file, and
pick the GPU nodes to register. Lease them to the platform, then add a search
space and a campaign. Bare-metal boxes over ssh: Resources > Add machine.
EOF
fi
cat <<EOF

Search policies: $SCRIPT policy policies/random-search$([ "$MODE" = demo ] || echo " --registry <registry your GPU nodes pull from>")
Your own values go in ${STATE#"$REPO/"}/llm-autotune.custom.yaml and
${STATE#"$REPO/"}/llm-bench.custom.yaml; run $SCRIPT again to apply them.
More in docs/after-installing.md.
EOF
if [ "$ACCESS" != port-forward ] || { [ "$ui" = 1 ] && [ -t 1 ]; }; then
  open_ui
else
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  cat <<EOF

  LLM AutoTune   admin / $AUTOTUNE_ADMIN_PASSWORD
  LLMBench       $LLMBENCH_ADMIN_EMAIL / $LLMBENCH_ADMIN_PASSWORD

Open the UIs with: $SCRIPT ui
EOF
fi
