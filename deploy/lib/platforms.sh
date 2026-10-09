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

action=up ui=1 yes=0 kind=0 context="" registry="" policy_dir="" policy_name="" gpus=0 needs_model=0
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
  rm -f "$STATE/secrets.env" "$STATE/context" "$STATE"/*.values.yaml
  if [ "$kind" = 1 ] && kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
    kind delete cluster --name "$KIND_CLUSTER"
  fi
  echo "Done."
  exit 0
fi

[ -f "$STATE/secrets.env" ] || [ "$action" = up ] ||
  die "nothing installed from here yet; run $SCRIPT first"

# -- ui ------------------------------------------------------------------------

open_ui() {
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  cat <<EOF

  LLM AutoTune   http://localhost:$AUTOTUNE_PORT   admin / $AUTOTUNE_ADMIN_PASSWORD
  LLMBench       http://localhost:$LLMBENCH_PORT   $LLMBENCH_ADMIN_EMAIL / $LLMBENCH_ADMIN_PASSWORD

Port-forwarding both UIs; Ctrl-C stops it ($SCRIPT ui starts it again).
EOF
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

# A deployment you keep runs engines on GPUs: say so now if there are none.
gpu_nodes=0
gpu_runtime=""
if [ "$MODE" = install ]; then
  gpu_nodes=$(k get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' |
    awk '$1 > 0 {n++} END {print n + 0}')
  if k get runtimeclass nvidia >/dev/null 2>&1; then gpu_runtime=nvidia; fi
  if [ "$gpu_nodes" = 0 ]; then
    printf '\nNo node in %s offers nvidia.com/gpu, so runs here would wait for cards.\n' "$CTX"
    printf 'GPUs can also come from another cluster or from ssh machines (docs/after-installing.md);\n'
    printf 'to try the platform without GPUs, use deploy/demo.sh instead.\n'
    if [ "$yes" != 1 ]; then
      [ -t 0 ] || die "no GPU nodes in $CTX; pass --yes to install anyway"
      printf 'Install anyway? [y/N] '
      read -r reply
      case "$reply" in y|Y|yes) ;; *) die "aborted" ;; esac
    fi
  fi
fi

mkdir -p "$STATE"
chmod 700 "$STATE"
echo "$CTX" > "$STATE/context"

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
    git -C "$src.partial" fetch -q --depth 1 https://github.com/modelsphere/llm-bench tag "v$LLMBENCH_VERSION"
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

# Values files hold the secrets, so they stay out of the process list.
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
} > "$STATE/llm-bench.values.yaml"
{
  cat <<EOF
jwtSecret: "$AUTOTUNE_JWT_SECRET"
adminPassword: "$AUTOTUNE_ADMIN_PASSWORD"
publicUiUrl: "http://localhost:$AUTOTUNE_PORT"
postgresql:
  password: "$AUTOTUNE_POSTGRES_PASSWORD"
llmbench:
  url: http://llm-bench-backend.$BENCH_NS:8000
  webUrl: "http://localhost:$LLMBENCH_PORT"
  apiKey: "$SERVICE_API_KEY"
EOF
  if [ "$MODE" = demo ]; then
    printf 'mockModel:\n  image: "%s"\n' "$mock_image"
  else
    # Runs land on this cluster's GPU nodes. Engine logs need a ReadWriteMany
    # volume, which a cluster may not have: off until the custom values say.
    printf 'gpuCluster:\n  inCluster: true\n  runtimeClass: "%s"\nrunLogs:\n  enabled: false\n' "$gpu_runtime"
  fi
} > "$STATE/llm-autotune.values.yaml"

# First pulls of the images can take a while on a slow connection; nothing
# here fails before this budget runs out.
WAIT=20m

say "Installing LLMBench into namespace $BENCH_NS"
h upgrade --install llm-bench "$bench_chart" -n "$BENCH_NS" --create-namespace --timeout "$WAIT" \
  -f "$STATE/llm-bench.values.yaml" -f "$STATE/llm-bench.custom.yaml" >/dev/null
for d in postgres redis backend worker frontend; do
  k -n "$BENCH_NS" rollout status "deploy/llm-bench-$d" --timeout="$WAIT"
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
h upgrade --install llm-autotune "$CHART" -n "$TUNE_NS" --create-namespace --timeout "$WAIT" \
  ${demo_values[@]+"${demo_values[@]}"} -f "$STATE/llm-autotune.values.yaml" \
  -f "$STATE/llm-autotune.custom.yaml" >/dev/null
k -n "$TUNE_NS" rollout status statefulset/llm-autotune-postgresql --timeout="$WAIT"
for d in api worker frontend; do k -n "$TUNE_NS" rollout status "deploy/llm-autotune-$d" --timeout="$WAIT"; done
if k -n "$TUNE_NS" get ds/llm-autotune-mock-model >/dev/null 2>&1; then
  k -n "$TUNE_NS" rollout status ds/llm-autotune-mock-model --timeout="$WAIT"
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
        "deploy_branch": "", "auto_promote": False,
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
GPU nodes found: $gpu_nodes. In LLM AutoTune, on Resources, use Refresh capacity on
local-cluster and then Lease to platform; then add a search space and a campaign.
EOF
fi
cat <<EOF

Search policies: $SCRIPT policy policies/random-search
Your own values go in ${STATE#"$REPO/"}/llm-autotune.custom.yaml and
${STATE#"$REPO/"}/llm-bench.custom.yaml; run $SCRIPT again to apply them.
More in docs/after-installing.md.
EOF
if [ "$ui" = 1 ] && [ -t 1 ]; then
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
