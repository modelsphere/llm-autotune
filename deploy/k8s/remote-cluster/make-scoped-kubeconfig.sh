#!/usr/bin/env bash
#
# Build a SCOPED kubeconfig for the autotune backend's ServiceAccount, so the
# backend authenticates as that SA (tuningruns + pods only) — never as admin.
#
# Run where you have admin/cluster access (e.g. the bastion), AFTER applying
# deploy/k8s/backend-rbac.yaml. It uses your CURRENT kubectl context to read the
# cluster CA + server, and reads the SA's NON-EXPIRING token from its Secret; the
# output kubeconfig contains only that token.
#
#   deploy/k8s/make-scoped-kubeconfig.sh [-n namespace] [-s serviceaccount] \
#       [-k tokensecret] [-o out.kubeconfig]
#
# Then point the backend at it:
#   AUTOTUNE_K8S_API_MODE=client
#   AUTOTUNE_K8S_KUBECONFIG=/abs/path/out.kubeconfig
#   AUTOTUNE_K8S_NAMESPACE=<namespace>
#
# The backend is normally a REMOTE client of the GPU cluster, so this scoped
# kubeconfig is the usual path regardless of where the backend runs. Only if the
# backend runs as a pod INSIDE the GPU cluster can you skip this: set
# serviceAccountName: autotune-backend on the pod and AUTOTUNE_K8S_IN_CLUSTER=true.
set -euo pipefail

NS=autotune
SA=autotune-backend
SECRET=autotune-backend-token        # the non-expiring token Secret (backend-rbac.yaml)
OUT=autotune-backend.kubeconfig

while getopts "n:s:k:o:h" opt; do
  case "$opt" in
    n) NS=$OPTARG ;;
    s) SA=$OPTARG ;;
    k) SECRET=$OPTARG ;;
    o) OUT=$OPTARG ;;
    *) echo "usage: $0 [-n namespace] [-s serviceaccount] [-k tokensecret] [-o out]" >&2; exit 2 ;;
  esac
done

CTX=$(kubectl config current-context)
CLUSTER=$(kubectl config view -o jsonpath="{.contexts[?(@.name=='$CTX')].context.cluster}")
SERVER=$(kubectl config view -o jsonpath="{.clusters[?(@.name=='$CLUSTER')].cluster.server}")
CADATA=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='$CLUSTER')].cluster.certificate-authority-data}")

if [ -z "$SERVER" ] || [ -z "$CADATA" ]; then
  echo "error: could not read server / certificate-authority-data from context '$CTX'." >&2
  echo "       (if your kubeconfig uses a CA *file* instead of -data, embed it manually.)" >&2
  exit 1
fi

# Read the SA's long-lived (non-expiring) token from its Secret. The token
# controller populates it shortly after the Secret is created, so wait briefly.
TOKEN=""
for _ in $(seq 1 10); do
  TOKEN=$(kubectl get secret "$SECRET" -n "$NS" -o jsonpath="{.data.token}" 2>/dev/null | base64 -d 2>/dev/null || true)
  [ -n "$TOKEN" ] && break
  sleep 1
done
if [ -z "$TOKEN" ]; then
  echo "error: token secret '$SECRET' in namespace '$NS' is empty or missing." >&2
  echo "       apply deploy/k8s/backend-rbac.yaml first — it creates the Secret." >&2
  exit 1
fi

cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: cluster
  cluster:
    server: $SERVER
    certificate-authority-data: $CADATA
users:
- name: $SA
  user:
    token: $TOKEN
contexts:
- name: $SA
  context:
    cluster: cluster
    user: $SA
    namespace: $NS
current-context: $SA
EOF
chmod 600 "$OUT"

echo "wrote $OUT  (context=$SA, namespace=$NS)"
echo "verify it is properly scoped:"
echo "  KUBECONFIG=$OUT kubectl auth can-i --list -n $NS"
echo "  KUBECONFIG=$OUT kubectl auth can-i get pods -n kube-system   # -> should be 'no'"
