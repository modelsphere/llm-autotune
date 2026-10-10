#!/usr/bin/env bash
# Prepare a GPU cluster for LLM AutoTune: run it once, with an admin kubeconfig
# for that cluster, and add the cluster on the platform's Resources page with
# the kubeconfig it writes.
#
#   deploy/gpu-cluster.sh                 create the namespace and a scoped account, write the kubeconfig
#   deploy/gpu-cluster.sh remove          revoke the account (the namespace and its runs stay)
#
# The platform gets one namespace for its engine and policy pods, and a
# ServiceAccount that may manage only those (Deployments, Services, Jobs,
# TuningRuns), read their pods, logs and events, and read nodes, which is how
# it registers the cluster's GPU nodes and their cards. It never writes a node.
# The kubeconfig it writes carries only that account's token.
#
# Options:
#   --context NAME     the kubectl context of the GPU cluster (default: the current one)
#   --namespace NS     the namespace engine pods run in (default: llm-autotune-runs).
#                      Every name derives from it, so two platforms can share a
#                      cluster by using two namespaces.
#   --out FILE         where to write the kubeconfig (default: <namespace>.kubeconfig)
#   --yes              do not ask before acting on the cluster
set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
command -v kubectl >/dev/null 2>&1 || die "kubectl is required"

action=up context="" ns=llm-autotune-runs out="" yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    remove) action=remove ;;
    --context) [ $# -ge 2 ] || die "--context needs a value"; context=$2; shift ;;
    --namespace|-n) [ $# -ge 2 ] || die "--namespace needs a value"; ns=$2; shift ;;
    --out|-o) [ $# -ge 2 ] || die "--out needs a value"; out=$2; shift ;;
    --yes|-y) yes=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
case "$ns" in
  *[!a-z0-9-]*|-*|*-|"") die "--namespace must be a DNS label: lowercase letters, digits and '-'" ;;
esac

ctx=${context:-$(kubectl config current-context 2>/dev/null)} ||
  die "no current kubectl context: point kubectl at the GPU cluster, or pass --context"
k() { kubectl --context "$ctx" "$@"; }
server=$(k config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[ -n "$server" ] || die "context $ctx has no cluster server"

SA=llm-autotune
NODES_ROLE="llm-autotune-nodes-read-$ns"  # cluster-scoped: the namespace keeps it unique
TOKEN_SECRET=llm-autotune-token
out=${out:-$ns.kubeconfig}

if [ "$yes" != 1 ]; then
  [ -t 0 ] || die "pass --yes to act on $server without asking"
  printf '%s the platform account in namespace %s on %s (context %s)? [y/N] ' \
    "$([ "$action" = remove ] && echo Remove || echo Create)" "$ns" "$server" "$ctx"
  read -r reply
  case "$reply" in y|Y|yes) ;; *) die "aborted" ;; esac
fi

if [ "$action" = remove ]; then
  say "Revoking the platform's access to $server"
  k delete clusterrolebinding "$NODES_ROLE" --ignore-not-found
  k delete clusterrole "$NODES_ROLE" --ignore-not-found
  k -n "$ns" delete secret "$TOKEN_SECRET" --ignore-not-found
  k -n "$ns" delete rolebinding,role,serviceaccount "$SA" --ignore-not-found
  echo "Done. Namespace $ns and anything still running in it are left; delete it with:"
  echo "  kubectl --context $ctx delete namespace $ns"
  exit 0
fi

for check in "create clusterrolebindings" "create namespaces"; do
  # shellcheck disable=SC2086
  [ "$(k auth can-i $check 2>/dev/null)" = yes ] ||
    die "context $ctx cannot $check; run this with an admin kubeconfig for the GPU cluster"
done

say "Creating namespace $ns and the platform's account on $server"
k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $ns
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: $SA
  namespace: $ns
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: $SA
  namespace: $ns
rules:
  # An engine is a Deployment and a NodePort Service the platform manages.
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  # Its pods, their logs and Warning events: where a run landed and why one
  # never started.
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "create", "delete"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list"]
  # A search policy runs to completion as a Job.
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "delete"]
  # With the optional operator, an engine is a TuningRun instead.
  - apiGroups: ["tuning.modelsphere.dev"]
    resources: ["tuningruns"]
    verbs: ["create", "get", "list", "watch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: $SA
  namespace: $ns
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: $SA
subjects:
  - kind: ServiceAccount
    name: $SA
    namespace: $ns
---
# Nodes are cluster-scoped. Read-only: GPU count and card type for each node,
# and the address a NodePort is reached on.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $NODES_ROLE
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: $NODES_ROLE
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: $NODES_ROLE
subjects:
  - kind: ServiceAccount
    name: $SA
    namespace: $ns
---
# A non-expiring token for the account; revoke it with: $0 remove
apiVersion: v1
kind: Secret
metadata:
  name: $TOKEN_SECRET
  namespace: $ns
  annotations:
    kubernetes.io/service-account.name: $SA
type: kubernetes.io/service-account-token
EOF

token=""
for _ in $(seq 1 20); do
  token=$(k -n "$ns" get secret "$TOKEN_SECRET" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)
  [ -n "$token" ] && break
  sleep 1
done
[ -n "$token" ] || die "the token controller never filled secret $TOKEN_SECRET in $ns"

ca=$(k config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
if [ -z "$ca" ]; then
  ca_file=$(k config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority}')
  [ -n "$ca_file" ] && [ -f "$ca_file" ] || die "context $ctx names no certificate authority to embed"
  ca=$(base64 < "$ca_file" | tr -d '\n')
fi

umask 077
cat > "$out" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: gpu-cluster
  cluster:
    server: $server
    certificate-authority-data: $ca
users:
- name: $SA
  user:
    token: $token
contexts:
- name: $SA@$ns
  context:
    cluster: gpu-cluster
    user: $SA
    namespace: $ns
current-context: $SA@$ns
EOF

# The account works, and can do no more than it should.
KUBECONFIG=$out kubectl get nodes >/dev/null 2>&1 || die "the written kubeconfig cannot read nodes; check $out"
[ "$(KUBECONFIG=$out kubectl auth can-i get pods -n kube-system 2>/dev/null)" = no ] ||
  die "the written kubeconfig reaches beyond $ns; check the cluster's RBAC"
gpus=$(KUBECONFIG=$out kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' |
  awk '$1 > 0 {n++} END {print n + 0}')

cat <<EOF

Wrote $out: namespace $ns on $server, $gpus GPU node(s) visible.
In LLM AutoTune, open Resources > Add GPU cluster and upload it.
EOF
