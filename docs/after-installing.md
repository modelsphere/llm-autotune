# After installing

[Installing](install.md) leaves LLM AutoTune and LLMBench running on one
cluster, connected to each other, with GPU clusters added on **Resources**.
This page covers the next steps; each one is a value to set or a command to
run against the same install.

Everything here also applies to the no-GPU demo: use `deploy/demo.sh` instead
of `deploy/quickstart.sh`, and `.demo/` instead of `.quickstart/`.

## Changing a setting

The install script applies two values files last, after its own settings:

| file | release | all values |
|---|---|---|
| `.quickstart/llm-autotune.custom.yaml` | LLM AutoTune | [`deploy/helm/llm-autotune/values.yaml`](../deploy/helm/llm-autotune/values.yaml) |
| `.quickstart/llm-bench.custom.yaml` | LLMBench | LLMBench's [`values.yaml`](https://github.com/modelsphere/llm-bench/blob/main/deploy/helm/llm-bench/values.yaml) |

Edit a file, then run `deploy/quickstart.sh` again. Both releases are upgraded
in place and keep their data. The script never overwrites these files, and
`deploy/quickstart.sh down` keeps them.

Settings without a chart value (timeouts, the default nightly window, pull
secrets) are environment variables, set through `extraEnv`;
[Deploying](deploying.md#other-settings) lists the common ones.

Generated passwords and keys are in `.quickstart/secrets.env`. Keep this file:
`jwtSecret` also encrypts the GPU cluster kubeconfigs stored in AutoTune, and
LLMBench's `platformSecretKey` encrypts the endpoint keys it stores.

## Users

The install creates one admin. By default anyone who can reach the UI can
create an ordinary account on the sign-in page. To have admins add accounts
instead (**Account ▸ Users ▸ Add user**), turn sign-up off:

```yaml
# .quickstart/llm-autotune.custom.yaml
allowSignup: false
```

## Settings

An admin sets the platform's links to other systems on the **Settings** page
(user menu ▸ Settings). A value set there wins over the install's, which still
applies while a field is empty. The checks run from the platform, not the
browser, because the platform's API is what calls these systems.

- **VictoriaLogs:** the store replay datasets are sampled from. It overrides
  `llmbench.victoriaLogsUrl` ([Replay datasets](#replay-datasets)).
- **Model catalog server:** a model-catalog `catalog-server`. The catalog
  export reads which models it has at `<address>/index-v2.json`, instead of
  `catalogIndexUrl`. With the server's write key set too, the export can
  publish to it directly ([Publishing it to the model catalog](guide/results.md#publishing-it-to-the-model-catalog)).
  The key is a token from the server's tokens file. It is stored encrypted and
  never shown again.
- **LLM endpoints:** OpenAI-compatible endpoints (requests go to
  `<base URL>/chat/completions`), each registered once with its model and key.
  **Test** makes one short call from the platform. Report writing uses the
  endpoint chosen under **What uses which**. An agent policy chooses its
  endpoint in its own form on the **Policies** page: at launch the endpoint
  sets `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` in the policy's
  container, over the same names in its env, and the key is redacted from the
  launch command. An endpoint in use cannot be deleted.

## Search policies

Without a policy, a campaign tries every config in its search space. A policy
is a container that chooses the next config to try. The install registers
`random-search` from its published image on ghcr.io. Other policies, or this
one when the GPU nodes cannot reach ghcr.io, are built from source: the script
builds the image, pushes it to a registry the GPU nodes pull from, and
registers it.

```bash
deploy/quickstart.sh policy policies/random-search --registry registry.example.com/team
```

Select it in **New campaign ▸ Search ▸ Strategy**. Running the command again
after changing the policy builds a new image and updates the registration; the
install never replaces a policy already registered under that name.

- **Policies in this repository** are under `policies/` (the
  [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies)
  submodule, fetched if missing). `random-search` samples the space. `chaos`
  injects faults to test how the platform handles a misbehaving policy; each
  registration picks one fault:
  `deploy/quickstart.sh policy policies/chaos --name chaos-idle --env CHAOS_SCENARIO=idle`.
- **A new policy:** copy `policies/random-search`, replace its strategy
  ([policies/README.md](https://github.com/modelsphere/llm-autotune-policies#readme)),
  and run `deploy/quickstart.sh policy path/to/it`. The API it implements is
  [policy-contract.md](api/policy-contract.md). A policy that starts engines
  inside its own container, instead of asking the platform, registers with
  `--gpus --needs-model`.
- **Registry:** policies run on the GPU clusters, so the image goes through a
  registry. `--registry` is required the first time and remembered after.
  Only `deploy/demo.sh`, whose runs share its own cluster, loads the image
  directly into a local cluster (kind, minikube, k3d, Docker Desktop, OrbStack).
- **Without the script,** register an image through the API with a key from
  the **API keys** page:

  ```bash
  curl -X POST http://localhost:8080/api/policies -H "X-API-Key: $KEY" \
    -H 'Content-Type: application/json' -d '{"name": "random-search",
      "image": "<registry>/llm-autotune-policy-random-search:0.1.0",
      "gpus_in_container": false, "needs_model": false}'
  ```

A policy runs as a Job on a GPU cluster and calls the platform at
`publicApiUrl`, which the install sets to the UI address. A policy that asks
for no cards may run on any node; when only some nodes reach what it calls
(an agent policy's LLM endpoint, say), pin it with
`{name: AUTOTUNE_K8S_POLICY_NODE_SELECTOR, value: "kubernetes.io/hostname=<node>"}`
under `extraEnv`. That names a node of one cluster; a policy runs on its
machine's cluster, so with several GPU clusters set **Policy node selector**
on each cluster's settings (Resources), which takes precedence. If the GPU clusters
reach the platform at a different address, set `publicApiUrl` in
`llm-autotune.custom.yaml`.

## GPUs

The platforms run no engines themselves; GPUs come from the clusters and
machines added on **Resources**.

### GPU clusters

On the GPU cluster, with its admin kubeconfig:

```bash
deploy/gpu-cluster.sh                 # --namespace NS to choose the namespace
```

The script creates the namespace engine pods run in (`llm-autotune-runs`), a
ServiceAccount limited to what the platform needs there plus read-only access
to nodes, and `llm-autotune-runs.kubeconfig` holding only that account's
token. All names derive from the namespace, so two platforms can share a GPU
cluster with two namespaces. `deploy/gpu-cluster.sh remove` revokes the
account.

On **Resources ▸ Add GPU cluster**, upload the file, press **Connect**, and
select the GPU nodes to register. Each node becomes a machine with the GPU
count and type the node reports, leased to the platform unless you clear
**Lease them to the platform now**. **Clusters ▸ Nodes** registers nodes added
later and lists registered machines whose node has left the cluster.

Runs are launched as **llmservice** by default: each run is an LLMService that
[llm-operator](https://github.com/modelsphere/llm-operator) installs, the
engine chart with the run's recipe, exactly as a production deploy is. The
cluster needs llm-operator (the modelsphere stack installs it; **Connect** says
when it is missing). **Reach runs by** says how AutoTune and LLMBench reach a
run: by its **Service name**, which works only when they run in the same
cluster, or by **NodePort** on a **Public host**, a node address both can
reach. A tag next to it shows where the cluster is, told apart by its
certificate authority (the one in the kubeconfig against the one mounted into
AutoTune's own pod):

- **AutoTune's cluster**: either option works.
- **External cluster**: **Service name** is locked, and NodePort is picked
  for you. Saving it with Service name is refused, and a campaign's checks and
  the machine smoke test fail, because no run on it could be reached.
- **Location unknown**: the kubeconfig has no CA to compare.

The tag appears as soon as a kubeconfig is pasted, before anything is saved.
The
**deployment** (a Deployment and a NodePort Service, no operator) and
**custom** (TuningRun) kinds still work and are deprecated: they take engine
flags and env only, not pd or chart values, and are no longer extended.

For engines reached over the public network with the deprecated
**deployment** kind, in **Add GPU cluster** or **Clusters ▸ More ▸ Edit**, set **Public
host** to a node address reachable by both the AutoTune worker
and LLMBench. Enter only the host, without `http://`, a port or a path; the API
server address in the kubeconfig is not necessarily a service entry point.
Set **NodePort** to the exposed port, for example `31080`, to produce
`http://<public-host>:31080`. The platform creates the NodePort Service for
each run. A public NAT entry must forward that port to the same node port.

Use `0` for automatic port allocation when running concurrently. A fixed
NodePort is shared cluster-wide and can belong to only one Service, so fixed
ports require serial runs and cleanup of the previous Service. Fixed ports
must be in the cluster's configured range (normally `30000–32767`). Public host
and fixed port settings here do not override an Operator's endpoint in
**custom** mode. Worker processes adopt edits within the settings cache TTL
(15 seconds) on their next driver lookup; an existing Service keeps its actual
allocated port.

Network paths:

- AutoTune's pods reach the cluster's API server.
- LLMBench reaches the engines: by the Service's in-cluster name (llmservice,
  endpoint dns), or as a NodePort Service on a node's address.
- A search policy running on the GPU cluster reaches the platform at
  `publicApiUrl`.

A run mounts the model weights from its node as a hostPath at the campaign's
model path, so a campaign uses the machines (nodes) that hold the weights.
Engine images that need credentials use the cluster's **Image pull secrets**;
nodes with additional taints need **Extra tolerations** (both under
**Advanced** when adding the cluster, or **Clusters ▸ More ▸ Edit**).

The UI shows each run's engine log and each policy's log: live from the
cluster while the container runs, and the end of the log, stored in the
database, after it ends. The worker also saves it every two minutes while it
runs, so a pod that disappears early still leaves its last lines. To
keep complete logs as files, give the platform a ReadWriteMany volume, which
the install script leaves off:

```yaml
# .quickstart/llm-autotune.custom.yaml
runLogs:
  enabled: true
  storageClass: nfs-client
```

### Bare-metal machines over ssh

The worker connects over ssh and starts the engine with `docker run`. Give it
a key:

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

Run the script again. Add the public key to the ssh user's
`~/.ssh/authorized_keys` on the machine (which needs Docker and the NVIDIA
container toolkit), then **Add machine** on **Resources** with its address.
The platform reads the GPU count over ssh; enter it under **Advanced** if it
cannot. Lease the machine with nothing else running on its GPUs; ending the
lease stops only the platform's own containers
([Deploying](deploying.md#bare-metal-machines-over-ssh)).

## Production models

With real GPUs, a campaign serves your model with a real engine image (sglang
or vLLM) and your search space. The usual starting point is the configuration
production runs today: record it on **Baselines**, then **Tune from this**
drafts a campaign from it ([Baselines](guide/baselines.md)). On the campaign's
**Benchmark** step, describe the load (synthetic prompts or a replay of
recorded traffic) and AutoTune creates the benchmark on LLMBench.

## Ingress

On a remote cluster the install exposes both UIs on a NodePort of every node
and prints one node's address (`NODE_HOST=<address>` selects another). With
`--port-forward` it forwards them to localhost instead. For a hostname and
TLS, use an ingress; once `publicUiUrl` is set, the script stops managing UI
access.

```yaml
# .quickstart/llm-autotune.custom.yaml
ingress:
  enabled: true
  className: nginx
  host: autotune.example.com
publicUiUrl: https://autotune.example.com       # links other systems send people to
llmbench:
  webUrl: https://llmbench.example.com          # links to LLMBench from AutoTune's pages
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

## Replay datasets

A Replay workload sends recorded production requests, which LLMBench collects
from a VictoriaLogs store of the gateway's request logs
([Datasets](guide/datasets.md)). Run the install again with the store's
address, as LLMBench's pods reach it:

```bash
VICTORIALOGS_URL=http://victorialogs.logging:9428 deploy/quickstart.sh
```

This turns on LLMBench's dataset collector and its datasets volume, and sets
the address the dataset form starts from (`llmbench.victoriaLogsUrl`; without
it, the form starts from `http://victorialogs.logging:9428`). The script
remembers the address for later runs. An admin can also change the address
later on the [Settings](#settings) page, without running the install again.

The datasets volume is shared by LLMBench's backend, workers and collector.
On a cluster with one node the node's disk is enough; with more, set a
ReadWriteMany storage class as `datasets.storageClass` in
`.quickstart/llm-bench.custom.yaml`. Credentials for the store, if it needs
them, go in `datasetBuilder.auth` there.

## Other datasets

The default benchmark needs no dataset: LLMBench's throughput sweep generates
its prompts. Academic suites and replays of captured traffic files need a
datasets volume on LLMBench (`datasets.enabled` in
`.quickstart/llm-bench.custom.yaml`); see LLMBench's
[deploying guide](https://github.com/modelsphere/llm-bench/blob/main/docs/deploying.md#storage)
and [replay datasets](https://github.com/modelsphere/llm-bench/blob/main/docs/replay-datasets.md).
Academic suites are downloaded from Hugging Face; nothing else LLMBench runs
needs internet access.

## Deploying a winner

Open the winning row on a campaign's leaderboard. The run details show the
**Engine command** (with a copy button), the image and the weights path, and
the results record the engine version and image digest. Apply these to the
serving deployment, then update the baseline so the next campaign measures
against it.

## The operator

To have runs reconciled by the TuningRun operator instead of created as
Deployments, install it ([operator/INSTALL.md](../operator/INSTALL.md)) and set
`operator.enabled: true`.

## Uninstalling

`deploy/quickstart.sh down` removes both releases and their data. The next
`deploy/quickstart.sh` installs from scratch with new passwords and still
applies the `*.custom.yaml` files. `deploy/demo.sh down --kind` also deletes
the demo's kind cluster.
