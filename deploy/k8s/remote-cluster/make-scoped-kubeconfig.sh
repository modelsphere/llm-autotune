#!/usr/bin/env bash
#
# Build a SCOPED kubeconfig for the platform's ServiceAccount on a GPU cluster
# it does not run in, so it authenticates with exactly what backend-rbac.yaml
# grants — never as admin.
#
# Run with an admin context for the GPU cluster, AFTER applying backend-rbac.yaml
# from this directory. It uses your CURRENT kubectl context to read the cluster
# CA + server, and reads the SA's NON-EXPIRING token from its Secret; the output
# kubeconfig contains only that token.
#
#   deploy/k8s/remote-cluster/make-scoped-kubeconfig.sh [-n namespace] \
#       [-s serviceaccount] [-k tokensecret] [-o out.kubeconfig]
#
# Then add the cluster on the platform's Resources page and paste the file in.
# If the platform runs inside the GPU cluster, skip this: install the chart with
# gpuCluster.inCluster=true and it grants its own ServiceAccount the same access.
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
  echo "       apply deploy/k8s/remote-cluster/backend-rbac.yaml first — it creates the Secret." >&2
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
