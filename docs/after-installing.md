# After installing

[Installing](install.md) leaves you with LLM AutoTune and LLMBench on one
cluster, connected, and GPU clusters added on **Resources**. This page takes
the install further one piece at a time: every step is a value you set, or a
command run against the same install.

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
policy is a container that decides what to try next instead. The install
registers `random-search` from its published image on ghcr.io. Any other
policy, or this one where the GPU nodes can't reach ghcr.io, is built from
source: the script builds it, pushes it to a registry your GPU clusters' nodes
pull from, and registers it:

```bash
deploy/quickstart.sh policy policies/random-search --registry registry.example.com/team
```

Then, in **New campaign ▸ Search ▸ Strategy**, pick it. A policy campaign also
takes a time budget on the same step. Running the command again after changing
the policy builds a new image and points the registration at it; a policy
already registered under a name is never replaced by the install.

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
- **The image goes through a registry**, because policies run on the GPU
  clusters: `--registry` is needed the first time, and the script remembers
  it. Only `deploy/demo.sh`, whose runs share its own cluster, loads the image
  straight into a local one (kind, minikube, k3d, Docker Desktop, OrbStack).
- **Without the script**, register an image with the API, using a key from the
  **API Keys** page:

  ```bash
  curl -X POST http://localhost:8080/api/policies -H "X-API-Key: $KEY" \
    -H 'Content-Type: application/json' -d '{"name": "random-search",
      "image": "<registry>/llm-autotune-policy-random-search:0.1.0",
      "gpus_in_container": false, "needs_model": false}'
  ```

A policy runs as a Job on a GPU cluster, next to the runs, and calls the
platform back at `publicApiUrl`, which the quickstart sets to the UI's address.
If the GPU clusters reach the platform by another address, set `publicApiUrl`
to it in `llm-autotune.custom.yaml`.

## GPUs

The platforms run no engines themselves: GPUs come from the clusters and
machines you add on **Resources**.

### A GPU cluster

On the GPU cluster, with its admin kubeconfig:

```bash
deploy/gpu-cluster.sh                 # --namespace NS to choose the namespace
```

It creates the namespace engine pods run in (`llm-autotune-runs`) and an
account allowed only what the platform needs there, plus read-only access to
nodes, and writes `llm-autotune-runs.kubeconfig` holding only that account's
token. Every name derives from the namespace, so two platforms can share a GPU
cluster with two namespaces. `deploy/gpu-cluster.sh remove` revokes the account.

On **Resources**, **Add GPU cluster**: upload the file, **Connect**, and tick
the GPU nodes to register. Each becomes a machine pinned to its node, with the
card count and type the node reports; **Lease to platform** on a machine lets
campaigns use it. Under **Clusters**, a cluster's **Nodes** registers nodes added
since and lists registered ones that left.

Three things must reach across:

- AutoTune's pods reach the cluster's API server.
- LLMBench reaches the runs: an engine is a NodePort Service, reached on a
  node's address.
- A search policy running there reaches the platform at `publicApiUrl`. The
  install script sets it to the UI's address, which serves the API too.

A run mounts its model weights from its node, as a hostPath at the campaign's
model path, so a campaign pins the machines (nodes) that hold them. Engine
images that need credentials take the cluster's **Image pull secrets**, and
nodes with extra taints its **Extra tolerations** (both under **Advanced**
when adding it, or **Clusters ▸ Edit**).

Engine logs are off by default, because they need a ReadWriteMany volume. With
such a storage class:

```yaml
# .quickstart/llm-autotune.custom.yaml
runLogs:
  enabled: true
  storageClass: nfs-client
```

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
