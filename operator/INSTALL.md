# Installing the autotune operator

For the administrator of a cluster the autotune platform wants to run
experiments on. It describes exactly what lands on your cluster, what it is
allowed to do, and how to take it back off.

The operator does one thing: it turns a `TuningRun` — "serve this image with
these arguments on N GPUs" — into a Deployment plus a Service, watches the pod,
and writes back where the endpoint is and why it failed. It is the piece that
lets the platform tune model-serving configs without holding credentials to
create Deployments on your cluster itself.

## What gets installed

Everything below is in `dist/install.yaml`, one file you generate from this
directory (see [Install](#install)) and apply in one command. Nothing else is
created, now or later, except the `TuningRun` workloads themselves (in whichever
namespace the platform is granted).

| kind | name | scope |
|---|---|---|
| CustomResourceDefinition | `tuningruns.tuning.llm-autotune.io` | cluster (the object itself is **namespaced**) |
| Namespace | `autotune-operator-system` | — |
| ServiceAccount | `autotune-operator-controller-manager` | namespaced |
| Deployment | `autotune-operator-controller-manager` (1 replica) | namespaced |
| Role + RoleBinding | `autotune-operator-leader-election-role` | namespaced (leases/configmaps/events, for leader election) |
| ClusterRole + binding | `autotune-operator-manager-role` | **cluster-wide — see below** |
| ClusterRole + binding | `autotune-operator-metrics-auth-role` | cluster-wide: `tokenreviews`/`subjectaccessreviews` create, so the metrics endpoint can authenticate callers |
| Service | `autotune-operator-controller-manager-metrics-service` (:8443) | namespaced |

The manager image is `gcr.io/distroless/static:nonroot` holding one static Go
binary. It runs as non-root (uid 65532) with a read-only root filesystem, all capabilities
dropped, `seccompProfile: RuntimeDefault`, and requests 10m CPU / 64Mi memory
(limits 500m / 128Mi) — it satisfies the **restricted** Pod Security Standard.

## What it is allowed to do — the part worth reviewing

`autotune-operator-manager-role` is a **ClusterRole**, and today the manager
watches all namespaces:

| API group | resources | verbs | why |
|---|---|---|---|
| `tuning.llm-autotune.io` | `tuningruns`, `/status`, `/finalizers` | full | its own CRD |
| `apps` | `deployments` | create, delete, get, list, watch, update, patch | a TuningRun *is* a Deployment plus a Service |
| `""` | `services` | create, delete, get, list, watch, update, patch | the endpoint a benchmark drives |
| `""` | `pods` | get, list, watch | read phase and failure reason (`OOMKilled`, `ImagePullBackOff`, `Unschedulable`) back into status |
| `""` | `nodes` | get, list, watch | resolve a NodePort endpoint to the InternalIP of the node the pod actually landed on |

Two honest caveats:

- **It is cluster-wide, not namespace-scoped.** The manager has no
  `--namespace` flag yet, so it could create a Deployment in any namespace. In
  practice it only ever acts on namespaces where a `TuningRun` exists, and only
  the platform's own credential can create those. If you need it confined,
  say so — scoping the cache and swapping the ClusterRole for a Role in one
  namespace is a change we are willing to make, not a redesign.
- **It never touches nodes, quotas, or anything it did not create.** The node
  grant is read-only, and every object it creates is owner-referenced to its
  `TuningRun`, so deleting the run deletes the workload.

## Install

No operator image is published. Build one into a registry your cluster pulls
from, render the manifest against it, and apply that:

```sh
IMG=<your-registry>/llm-autotune-operator:0.1.0
make docker-build docker-push IMG=$IMG   # or `make docker-buildx IMG=$IMG` for several platforms
make build-installer IMG=$IMG            # writes dist/install.yaml; the Makefile fetches its own tools
kubectl apply -f dist/install.yaml
```

The image is named in that file, so what you review is what runs. Pin it to a
digest for a reproducible rollout.

If the manager's own image needs a pull secret, add one to the
`autotune-operator-controller-manager` ServiceAccount; the *engine* images the
platform runs carry their own (`AUTOTUNE_K8S_IMAGE_PULL_SECRETS`).

## Verify

```sh
kubectl get crd tuningruns.tuning.llm-autotune.io
kubectl -n autotune-operator-system get pods          # 1/1 Running
kubectl -n autotune-operator-system logs deploy/autotune-operator-controller-manager
```

A complete smoke test that uses no GPU (it will stay Pending or fail on a
missing image — the point is only that the operator reconciles it and writes
status):

```sh
kubectl apply -f config/samples/tuning_v1alpha1_tuningrun.yaml
kubectl get tuningruns -A          # PHASE / GPUs / ENDPOINT columns
kubectl delete -f config/samples/tuning_v1alpha1_tuningrun.yaml
```

## Uninstall

```sh
kubectl delete -f dist/install.yaml
```

That removes the CRD, and with it every `TuningRun` and — through owner
references — every Deployment and Service the operator created. Nothing of ours
survives it.

## What the platform needs in addition

The operator alone is not enough: the autotune backend also needs a credential
of its own to create `TuningRun` objects. That is a separate, **namespaced**
ask — see [`deploy/k8s/remote-cluster/`](../deploy/k8s/remote-cluster/) in this
repository (namespace, ServiceAccount, Role, token, and a script that turns them
into a scoped kubeconfig).

Backend settings that select this path:

```sh
AUTOTUNE_K8S_WORKLOAD_KIND=custom            # submit TuningRuns instead of Deployments
AUTOTUNE_K8S_NAMESPACE=autotune              # where they are created
AUTOTUNE_K8S_IMAGE_PULL_SECRETS=             # names, if your nodes lack registry creds
AUTOTUNE_K8S_MODEL_PVC=                      # a shared weights claim, if you have one
AUTOTUNE_K8S_MODEL_PVC_ROOT=/mnt/disk0/models  # host path that claim's root corresponds to
```

## Versions

| version | changes | compatibility |
|---|---|---|
| 0.1.0 | first public release: TuningRun → Deployment + Service, phase/endpoint status, TTL teardown, `spec.tolerations` (for tainted GPU pools), `spec.modelSubPath` (one shared weights PVC serving many models) | — |
