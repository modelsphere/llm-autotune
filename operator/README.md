# autotune-operator

A small Kubernetes operator that runs **one serving workload per autotune
experiment** on the GPU cluster, so the autotune platform can benchmark a
candidate config without holding `kubectl` against the cluster itself.

**Installing it on a cluster — yours or someone else's — is
[`INSTALL.md`](INSTALL.md)**: what lands, what it is allowed to do, and how to
take it off again. No image is published: you build one and render
`dist/install.yaml` against it with `make build-installer IMG=<image>`.

It introduces a single CRD, **`TuningRun`** (`tuning.modelsphere.dev/v1alpha1`). The
platform creates a `TuningRun`; the operator launches the workload, waits for it
to serve, publishes a reachable endpoint, and tears it down — and writes all of
that back into the object's `status`. That's the whole job.

## Why an operator (and why *thin*)

The platform's search loop — pick a config, benchmark it through LLMBench, score
it, decide the next config — is Python and stays where it is. What it should not
do is drive Kubernetes directly (apply Deployments, poll pods, resolve
NodePorts, chase RBAC) from an outside worker. This operator absorbs exactly that
k8s-native slice and nothing more:

- **In scope:** create the workload, report readiness, hand back an endpoint,
  garbage-collect on delete, enforce a TTL.
- **Out of scope:** benchmarking, scoring, dataset handling, config search — all
  of that stays in the platform. The operator never looks at a result.

A `TuningRun` is an ephemeral experiment, not a production deployment: it
deliberately does **not** touch whatever serves shipped models on the cluster
(routes, gateways, monitoring). Promoting a winner into production is a separate
concern, handled by the platform's promotion path.

## The `TuningRun` contract

`spec` is the request (the platform writes it, the operator only reads it).
`status` is the response (the operator writes it, the platform only reads it).
Agree on these fields and the two sides build independently.

### `spec` — what to run

| field | meaning |
|---|---|
| `image` | serving image, ideally digest-pinned (`registry.example.com/sglang@sha256:…`) |
| `command` / `args` | the engine argv, built by the platform's own engine adapter and passed through **verbatim** — the operator is engine-agnostic |
| `port` | HTTP port the engine serves on; drives the container port, readiness probe, and Service |
| `readinessPath` | readiness URL (default `/v1/models` — the OpenAI-style endpoint that answers only once weights are loaded) |
| `gpuCount` | GPUs requested as a **count** (`nvidia.com/gpu`); the scheduler places them, we never pin indices. `>0` also sets `runtimeClassName: nvidia` |
| `nodeSelector` | pin a card type, e.g. `nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB` |
| `modelHostPath` / `modelPVC` + `modelMountPath` | mount weights from a node hostPath (matches how the cluster serves today) or a PVC |
| `sharedMemoryMB` | size an in-memory `/dev/shm` for tensor-parallel engines |
| `env`, `imagePullSecrets`, `imagePullPolicy` | the usual container knobs |
| `serviceType` | `NodePort` (default, reachable off-cluster) or `ClusterIP` |
| `ttlSeconds` | safety net: the operator tears the run down after this long, so a platform crash can't leak GPUs |

A `TuningRun` is **immutable** — to change a config, the platform creates a new
run (with a new deterministic name like `autotune-run-<id>`), not edits an old
one.

### `status` — what happened

| field | meaning |
|---|---|
| `phase` | `Pending → Starting → Ready → Failed` / `Expired` (see below) |
| `endpoint` | base URL where the model answers, reachable per `serviceType` |
| `nodePort` | the allocated node port (NodePort mode) |
| `podName`, `nodeName` | where it landed (for logs/debugging) |
| `reason` | machine-readable failure class (`OOMKilled`, `ImagePullBackOff`, `CrashLoopBackOff`, `Unschedulable`, …) |
| `message`, `startTime`, `readyTime`, `observedGeneration`, `conditions` | detail + standard bookkeeping |

### Phase → the platform's launch state machine

| `status.phase` | platform `DeploymentState` | platform does |
|---|---|---|
| `Pending` / `Starting` | `STARTING` | keep polling (weights can take ~10 min) |
| `Ready` | `READY` | read `status.endpoint`, run LLMBench |
| `Failed` | `CRASHED` | fetch logs, classify via `status.reason`, record a failed run |
| *(object gone)* | `GONE` | teardown confirmed |
| `Expired` | `CRASHED`/`GONE` | TTL fired; treat as a lost run |

## How the controller works

Level-triggered reconcile (`internal/controller/tuningrun_controller.go`):

1. **Ensure** a 1-replica `Deployment` + a `Service` exist, both **owned by the
   `TuningRun`** via `ownerReferences` — so deleting the run garbage-collects the
   workload; there's no finalizer to get stuck.
2. **Observe** the newest pod and map it to a `phase` + `reason`. Readiness comes
   from the pod's own `Ready` condition (the kubelet running the `readinessPath`
   probe from inside the cluster), not an HTTP call from outside.
3. **Resolve** the endpoint: `http://<node InternalIP>:<nodePort>` for NodePort,
   the Service DNS name for ClusterIP.
4. **Enforce** `ttlSeconds`: past the deadline, tear the workload down and mark
   `Expired`.
5. **Write** status only when something changed, and requeue to re-observe.

The render (`buildDeployment` / `buildService`) and the phase mapping
(`phaseFromPod`) are pure functions, unit-tested directly with no cluster.

## Build / test / deploy

```sh
make generate manifests   # regenerate deepcopy, CRD, and RBAC from the Go markers
make build                # compile
go test ./internal/...    # fast unit tests (fake client; no envtest needed)

make docker-build docker-push IMG=<registry>/llm-autotune-operator:<version>
make install              # apply the CRD
make deploy IMG=…         # deploy the controller (namespace autotune-operator-system)

kubectl apply -k config/samples/   # a sample TuningRun
```

> This module pins `go 1.26`.

---

Scaffolded with kubebuilder `go.kubebuilder.io/v4`.
