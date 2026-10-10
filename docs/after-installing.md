# After installing

[`deploy/quickstart.sh`](../deploy/quickstart.sh) leaves you with LLM AutoTune
and LLMBench on one cluster, connected, with that cluster registered as the
machine pool `local-cluster`. This page takes the install further one piece at
a time: every step is a value you set, or a command run against the same
install.

Everything here works the same on the no-GPU demo: use `deploy/demo.sh` in
place of `deploy/quickstart.sh`, and `.demo/` in place of `.quickstart/`.

## Changing a setting

The script keeps two values files of yours and applies them last, after
everything it sets itself:

| file | release | every value is in |
|---|---|---|
| `.quickstart/llm-autotune.custom.yaml` | LLM AutoTune | [`deploy/helm/llm-autotune/values.yaml`](../deploy/helm/llm-autotune/values.yaml) |
| `.quickstart/llm-bench.custom.yaml` | LLMBench | LLMBench's [`values.yaml`](https://github.com/modelsphere/llm-bench/blob/main/deploy/helm/llm-bench/values.yaml) |

Edit one, then run `deploy/quickstart.sh` again. It upgrades both releases in
place and keeps their data. It never overwrites these two files;
`deploy/quickstart.sh down` keeps them too.

AutoTune settings the chart has no value for (timeouts, the default
nightly window, pull secrets…) are environment variables, set through
`extraEnv`; [deploying.md](deploying.md#other-settings) lists the ones installs
usually need.

The generated passwords and keys are in `.quickstart/secrets.env`. Keep that
file: `jwtSecret` also encrypts the cluster credentials you paste into
AutoTune, and LLMBench's `platformSecretKey` encrypts the endpoint keys it
stores.

## Search policies

A campaign with no policy tries every configuration in its search space. A
policy is a container that decides what to try next instead. Policies are built
from source, not pulled; the script builds one, loads it into the cluster and
registers it:

```bash
deploy/quickstart.sh policy policies/random-search
```

Then, in **New campaign ▸ Search ▸ Strategy**, pick it. A policy campaign also
takes a time budget on the same step. Running the command again after changing
the policy builds a new image and points the registration at it.

- **The policies in this repository** are under `policies/` (the
  [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies)
  submodule; the command fetches it if it is missing). `random-search` samples
  the space. `chaos` misbehaves on purpose, to show how the platform handles a
  broken policy; pick its fault at registration, one policy per fault:
  `deploy/quickstart.sh policy policies/chaos --name chaos-idle --env CHAOS_SCENARIO=idle`.
- **Your own policy**: copy `policies/random-search`, replace its strategy
  ([policies/README.md](https://github.com/modelsphere/llm-autotune-policies#readme)),
  and run `deploy/quickstart.sh policy path/to/it`. The contract it speaks is
  [policy-contract.md](api/policy-contract.md). A policy that starts engines
  itself, instead of asking the platform to, registers with `--gpus --needs-model`.
- **The image goes through a registry**: add `--registry <repo>` (one you can
  push to and the nodes can pull from) the first time; the script remembers it.
  Local clusters (kind, minikube, k3d, Docker Desktop, OrbStack) need none.
- **Without the script**, register an image with the API, using a key from the
  **API Keys** page:

  ```bash
  curl -X POST http://localhost:8080/api/policies -H "X-API-Key: $KEY" \
    -H 'Content-Type: application/json' -d '{"name": "random-search",
      "image": "<registry>/llm-autotune-policy-random-search:0.1.0",
      "gpus_in_container": false, "needs_model": false}'
  ```

A policy runs as a Job next to the runs and calls the platform back at
`publicApiUrl`. By default that is the API's in-cluster address, which works
for runs in the same cluster; when they land elsewhere (below), set
`publicApiUrl` to an address reachable from there.

## GPUs

`local-cluster` is the cluster the platform is installed in. On **Resources**,
**Re-read GPUs** (under **More**) reads its card count and type from the nodes, and **Lease
to platform** lets campaigns use it. One install can also use other clusters
and ssh machines.

### This cluster

The script runs engines on nodes that offer `nvidia.com/gpu`, with the
`nvidia` RuntimeClass when the cluster has one. To pin runs to some nodes, or
change either:

```yaml
# .quickstart/llm-autotune.custom.yaml
gpuCluster:
  nodeSelector: "nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3"
  # gpuResource: nvidia.com/gpu
  # runtimeClass: nvidia
```

A run mounts its model weights from the node, as a hostPath at the campaign's
model path, so the nodes it can land on must hold the weights there. To serve
weights from one shared volume instead, set `gpuCluster.modelPvc` to a
ReadOnlyMany/ReadWriteMany claim and `gpuCluster.modelPvcRoot` to the path the
campaigns' model paths are relative to. Engine images that need credentials
take a pull secret through `extraEnv` (`AUTOTUNE_K8S_IMAGE_PULL_SECRETS`).

Engine logs are off by default, because they need a ReadWriteMany volume. With
such a storage class:

```yaml
# .quickstart/llm-autotune.custom.yaml
runLogs:
  enabled: true
  storageClass: nfs-client
```

### Another cluster

On the GPU cluster, create a namespace, a ServiceAccount allowed only what the
platform needs, and a kubeconfig for it:

```bash
kubectl apply -f deploy/k8s/remote-cluster/backend-rbac.yaml
deploy/k8s/remote-cluster/make-scoped-kubeconfig.sh
```

On **Resources ▸ Clusters**, add the cluster with that kubeconfig, then
**Add machine** on it. Three things must reach across: AutoTune's pods reach that cluster's API
server; LLMBench reaches the runs there (a NodePort on a node's address, which
the cluster form lets you set); and a policy there reaches `publicApiUrl`.

### Bare-metal machines over ssh

The worker ssh'es in and runs the engine with `docker run`. Give it a key:

```bash
kubectl -n llm-autotune create secret generic autotune-worker-ssh \
  --from-file=id_ed25519=$HOME/.ssh/id_ed25519
```

```yaml
# .quickstart/llm-autotune.custom.yaml
worker:
  ssh:
    enabled: true
    existingSecret: autotune-worker-ssh
```

Run the script again, then **Add machine** on **Resources** with its address and
ssh user, and put the public half of that key in the user's
`~/.ssh/authorized_keys` there. The machine needs Docker and the NVIDIA
container toolkit. Lease it to the platform free; ending the lease stops only
the platform's own containers
([deploying.md](deploying.md#bare-metal-boxes-over-ssh)).

## Your own models

With a machine that has GPUs, a campaign serves your model with a real engine
image (sglang or vLLM) and your search space. The usual path starts from the
configuration production runs today: record it on **Baselines**, then **Tune
from this** drafts a campaign from it. [How it works](workflow.md) describes the
whole loop. On a campaign's **Benchmark** step, describe the load each candidate
gets (synthetic prompts, or a replay of recorded traffic) and AutoTune creates
the benchmark on LLMBench. Keep prompts short and concurrency low against the
mock engine, as the demo's `autotune-quickstart-v1` does.

## Reaching the UIs

On a remote cluster the script opens both UIs on a NodePort, on every node,
and prints one node's address (`NODE_HOST=<address>` picks another). With
`--port-forward` it forwards them to localhost instead, as it does on a
cluster on your own machine. For a name and TLS, put them behind an ingress:
once `publicUiUrl` is in your values, the script leaves the UIs to it.

```yaml
# .quickstart/llm-autotune.custom.yaml
ingress:
  enabled: true
  className: nginx
  host: autotune.example.com
publicUiUrl: https://autotune.example.com       # links other systems send people to
llmbench:
  webUrl: https://llmbench.example.com          # links to LLMBench in AutoTune's pages
```

```yaml
# .quickstart/llm-bench.custom.yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: llmbench.example.com
      paths: [{path: /, pathType: Prefix, service: frontend}]
```

## Datasets

The default benchmark needs none: LLMBench's throughput sweep generates its
prompts. For
the academic suites, and for replaying captured or live gateway traffic, give
LLMBench a datasets volume in `.quickstart/llm-bench.custom.yaml`
(`datasets.enabled`) and follow LLMBench's
[deploying guide](https://github.com/modelsphere/llm-bench/blob/main/docs/deploying.md#storage)
and [replay datasets](https://github.com/modelsphere/llm-bench/blob/main/docs/replay-datasets.md).
The academic suites are fetched from Hugging Face; nothing else LLMBench runs
needs it.

## Rolling out a winner

A campaign's winner is a run like any other: its page has the exact launch
command, the image digest and the engine version it ran with. Apply those to
your serving deployment, by hand or from your own pipeline.

## The operator

To have runs reconciled by the TuningRun operator instead of created as
Deployments, install it ([operator/INSTALL.md](../operator/INSTALL.md)) and set
`operator.enabled: true`.

## Starting over

`deploy/quickstart.sh down` removes both releases and their data; the next
`deploy/quickstart.sh` installs from scratch with new passwords, and still
applies your `*.custom.yaml` files. `deploy/demo.sh down --kind` also deletes
the demo's kind cluster.
