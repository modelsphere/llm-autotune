# Installing

LLM AutoTune uses two kinds of cluster:

- **The platform cluster** runs LLM AutoTune and LLMBench: the APIs, the
  worker, the UIs and their databases. It needs no GPUs.
- **GPU clusters** run the engines: each candidate config is an engine pod on
  a GPU node, and a search policy runs there too. GPU clusters are added from
  the UI after the install.

One cluster can be both; the steps are the same. Bare-metal GPU machines
reached over ssh are also supported ([below](#bare-metal-machines-over-ssh)).

## Requirements

- **Workstation:** `kubectl`, `helm` 3, `git`, `openssl`, and `docker` to
  build search policies.
- **Platform cluster:** a default StorageClass (both platforms keep a Postgres
  volume), nodes that can pull from Docker Hub or a mirror of it
  ([below](#clusters-without-docker-hub-access)), and a kubectl context that
  can create namespaces.
- **Each GPU cluster:**
  - the NVIDIA device plugin, so nodes report `nvidia.com/gpu`;
  - an admin kubeconfig, used once to create the platform's account;
  - the model weights on the GPU nodes, at the same path on each (runs mount
    them from the node);
  - an engine image (sglang or vLLM) the nodes can pull.
- **Network:**
  - the platform's pods reach each GPU cluster's API server and its nodes'
    NodePorts (engines are exposed on NodePorts);
  - the GPU nodes reach the platform's UI address, which a search policy uses
    to call the API.

## 1. Install the platforms

```bash
git clone https://github.com/modelsphere/llm-autotune
cd llm-autotune
deploy/quickstart.sh            # --context NAME to target another context
```

The script installs LLMBench (namespace `llm-bench`) and LLM AutoTune
(namespace `llm-autotune`) into the current kubectl context and connects them
with a generated service key. It asks for confirmation before installing into
a cluster that does not look local (`--yes` skips this). While it waits, it
reports stuck pods with the reason: an image pull failure, a crash with its
log, or a pod that cannot be scheduled.

At the end it prints both UI addresses and logins. Each UI is exposed on a
NodePort of every node, `http://<node address>:<port>`; `NODE_HOST=<address>`
selects the address printed, and `--port-forward` uses kubectl port-forwarding
instead. On a local cluster (kind, Docker Desktop) it port-forwards to
<http://localhost:8080> and <http://localhost:8081>.

| | sign in as |
|---|---|
| LLM AutoTune | `admin` and the printed password |
| LLMBench | `admin@example.com` and the printed password |

Passwords and keys are stored in `.quickstart/secrets.env`; keep this file.
`deploy/quickstart.sh ui` prints the addresses again.

## 2. Prepare a GPU cluster

With the GPU cluster's admin kubeconfig:

```bash
deploy/gpu-cluster.sh --context <gpu-cluster-context>
```

The script creates:

- a namespace for runs (`llm-autotune-runs`; `--namespace` to change it);
- a ServiceAccount that can manage only the platform's own workloads in that
  namespace and read nodes;
- `llm-autotune-runs.kubeconfig`, holding only that account's token. The script
  verifies that the token works and cannot reach outside its namespace.

If the API server's hostname resolves only through the workstation's
`/etc/hosts`, the kubeconfig uses its IP address instead, because the
platform's pods cannot resolve that name. `--api-server https://<address>:6443`
sets the address explicitly. `deploy/gpu-cluster.sh remove` revokes the
account.

## 3. Register the GPU nodes

In LLM AutoTune, open **Resources ▸ Add GPU cluster**:

1. Upload the kubeconfig and press **Connect**. The platform lists the
   cluster's GPU nodes and their cards.
2. Select the nodes and press **Register**. Each node becomes a machine pinned
   to it. **Lease them to the platform now** (selected by default) also leases
   them, so campaigns can use them immediately.

Image pull secrets and extra tolerations are under **Advanced**; the standard
GPU taint is tolerated automatically. **Clusters ▸ Nodes** registers nodes
added to the cluster later.

## 4. Search policies

Without a policy, a campaign tries every config in its search space. A policy
chooses which config to try next. The install registers `random-search` from
its published image (`ghcr.io/modelsphere/llm-autotune-policy-random-search`);
select it on a campaign's **Search** step. The policy pod runs on the GPU
cluster, so the GPU nodes must be able to pull from ghcr.io. If they cannot,
mirror the image and update it on the **Policies** page, or build it from
source:

```bash
deploy/quickstart.sh policy policies/random-search --registry registry.example.com/team
```

This builds `policies/random-search` (the
[llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies)
submodule, fetched if missing), pushes the image, and points the
`random-search` registration at it. The registry is remembered for later
builds. Writing a policy: [After installing](after-installing.md#search-policies).

## 5. First campaign

[First campaign](guide/first-campaign.md) walks through every step with
screenshots; the [user guide](guide/README.md) covers each page. In short:

1. **Search spaces ▸ New search space:** the engine settings to try. Start
   with a few values of one or two settings.
2. **Campaigns ▸ New campaign:**
   - **Model:** container image and model path on the nodes.
   - **Machines:** the leased machines to use.
   - **Search:** the search space, and optionally a policy.
   - **Benchmark:** a synthetic sweep (input and output tokens, concurrency
     levels, requests per slot) or a replay, and the objective.
   - **Schedule:** a nightly window, or no window.
   - **Check:** on a GPU cluster, probe pods on each node pull the engine and
     policy images and mount the model path, so a wrong tag or path is
     reported before the first run.
3. **Create campaign**, then **Start** on its page (or **Run now** for a
   scheduled campaign). **Pause** lets current runs finish; **Stop** ends them.

## Local setup with kind

The same steps work with two [kind](https://kind.sigs.k8s.io/) clusters on one
machine: one for the platforms, and one that simulates a GPU cluster with the
mock engine. Measurements from the mock engine are not meaningful; the setup
exercises the install and the full tuning loop. Allow Docker about 8 GB.

```bash
kind create cluster --name autotune                    # the platform cluster
deploy/quickstart.sh --context kind-autotune
deploy/dev/mock-gpu-cluster.sh                         # kind-autotune-gpu: 4 mock GPUs, the mock engine
deploy/gpu-cluster.sh --context kind-autotune-gpu      # writes llm-autotune-runs.kubeconfig
```

Then follow steps 3 to 5 at <http://localhost:8080>:

- **Add GPU cluster:** upload `llm-autotune-runs.kubeconfig`. `gpu-cluster.sh`
  writes the cluster's address on the Docker network, which the platform's
  pods can reach, instead of its localhost port.
- **Campaign:** engine `sglang`, the image and model path that
  `mock-gpu-cluster.sh` prints, and a search space over the mock engine's own
  parameters, such as `mock_token_ms: [2, 20]`. Keep the benchmark small (128
  input and 32 output tokens, concurrency `1, 4`, 5 requests per slot), since
  the benchmark client runs on the same machine.
- **Datasets** (optional): `deploy/dev/demo-traffic.sh --context kind-autotune`
  loads a day of made-up gateway traffic into a VictoriaLogs store on the
  platform cluster. Run the install again with
  `VICTORIALOGS_URL=http://victorialogs.victorialogs:9428` to turn on
  LLMBench's collector, then use **Datasets** (top menu) and a
  Replay workload.

If the workstation uses a local proxy (`HTTPS_PROXY=http://127.0.0.1:…`),
create the platform cluster without it:
`env -u HTTPS_PROXY -u https_proxy kind create cluster --name autotune`. kind
passes the proxy to its nodes, where 127.0.0.1 is the node itself, so every
image pull fails. `mock-gpu-cluster.sh` and `deploy/demo.sh --kind` drop such
a proxy automatically.

`deploy/quickstart.sh down`, `deploy/dev/mock-gpu-cluster.sh down` and
`kind delete cluster --name autotune` remove everything.

## Upgrading and uninstalling

- Run `deploy/quickstart.sh` again to apply changes or upgrade to a newer
  release. Both releases are upgraded in place and keep their data.
- Custom values go in `.quickstart/llm-autotune.custom.yaml` and
  `.quickstart/llm-bench.custom.yaml`, applied last.
- `deploy/quickstart.sh down` removes both releases and their data.

## Clusters without Docker Hub access

Mirror the images into a registry the cluster can pull from, and point the
charts at it:

```yaml
# .quickstart/llm-autotune.custom.yaml
image:
  registry: registry.example.com/mirror
postgresql:
  image: registry.example.com/mirror/postgres:16-alpine
```

```yaml
# .quickstart/llm-bench.custom.yaml
image:
  backend:
    repository: registry.example.com/mirror/llm-bench-backend
  frontend:
    repository: registry.example.com/mirror/llm-bench-frontend
postgres:
  image:
    registry: registry.example.com
    repository: mirror/postgres
redis:
  image:
    registry: registry.example.com
    repository: mirror/redis
```

Mirrored images keep their tags (`4pdosc/llm-autotune-backend:<version>` and
so on), so mirror the versions the charts reference. The search policy image
is on ghcr.io; mirror it as well and update it on the **Policies** page. If
GitHub is also unreachable, check out llm-bench at the matching tag and pass
`LLMBENCH_CHART=<clone>/deploy/helm/llm-bench`.

## Bare-metal machines over ssh

The worker connects over ssh and starts the engine with `docker run`; see
[After installing](after-installing.md#bare-metal-machines-over-ssh).

## Troubleshooting

- **A pod is stuck during the install:** the script prints the reason.
  `ImagePullBackOff` means the nodes cannot reach the registry (see above) or,
  on kind, a proxy on 127.0.0.1 ([above](#local-setup-with-kind)). `Pending`
  usually means there is no default StorageClass.
- **Connect fails when adding a GPU cluster:** the error states whether the
  API server's name did not resolve or the server did not respond from the
  platform's pods. Run `gpu-cluster.sh` again with `--api-server`.
- **A run never becomes ready:** the run's details and log give the reason
  (image pull, missing model path, no free GPUs). The **Check** step of New
  campaign detects most of these before a campaign starts.
- **A policy campaign stays in *starting*:** the policy panel on the campaign
  page shows what the policy pod is waiting for.

More symptoms and causes: [Troubleshooting](guide/troubleshooting.md).
