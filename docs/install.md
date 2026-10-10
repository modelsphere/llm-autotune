# Installing

LLM AutoTune runs on two kinds of cluster:

- **The platform cluster** holds LLM AutoTune and LLMBench: the API, the
  worker, the UIs and their databases. It needs no GPUs.
- **GPU clusters** hold the runs: each candidate config is an engine pod on a
  GPU node, and a search policy runs there too. Add as many as you have, after
  the install, from the UI.

They can be the same cluster; the steps don't change. Bare-metal GPU boxes
reached over ssh work too ([below](#bare-metal-machines-over-ssh)).

## What you need

- **On your machine:** `kubectl`, `helm` 3, `git` and `openssl`, and `docker`
  if you build search policies.
- **The platform cluster:** a default StorageClass (both platforms keep a
  Postgres volume), and nodes that can pull from Docker Hub, or a mirror of it
  ([below](#a-cluster-that-cannot-reach-docker-hub)). Your kubectl context can
  create namespaces there.
- **Each GPU cluster:**
  - the NVIDIA device plugin, so nodes report `nvidia.com/gpu`;
  - an admin kubeconfig, used once to create the platform's account;
  - the model weights on the GPU nodes, at the same path on each, since runs
    mount them from the node;
  - an engine image (sglang or vLLM) the nodes can pull.
- **Reachability:**
  - The platform's pods reach each GPU cluster's API server and its nodes'
    NodePorts (an engine is reached on one).
  - The GPU nodes reach the platform's UI address. A search policy calls the
    API through it.

## 1. Install the platforms

```bash
git clone https://github.com/modelsphere/llm-autotune
cd llm-autotune
deploy/quickstart.sh            # --context NAME for a context other than the current one
```

It installs LLMBench (namespace `llm-bench`) and LLM AutoTune (namespace
`llm-autotune`) into the cluster kubectl points at, and wires them with a
generated service key. It asks before touching a cluster that doesn't look
local (`--yes` skips the question). While it waits, it prints any pod that is
stuck, with the reason: an image that won't pull, a crash with its log, a pod
nothing schedules.

At the end it prints both UIs' addresses and logins. Each UI is on a NodePort
of every node, `http://<node address>:<port>`. `NODE_HOST=<address>` picks
which address is printed, and `--port-forward` reaches the UIs through kubectl
instead. On a cluster on your own machine (kind, Docker Desktop) it
port-forwards to <http://localhost:8080> and <http://localhost:8081>.

| | sign in as |
|---|---|
| LLM AutoTune | `admin` and the printed password |
| LLMBench | `admin@example.com` and the printed password |

The passwords and keys are kept in `.quickstart/secrets.env`; keep the file.
`deploy/quickstart.sh ui` prints the addresses again.

## 2. Prepare a GPU cluster

With that cluster's admin kubeconfig:

```bash
deploy/gpu-cluster.sh --context <gpu-cluster-context>
```

It creates:
- a namespace for the runs (`llm-autotune-runs`; `--namespace` to choose);
- an account that may manage only the platform's own workloads there, and read
  nodes;
- `llm-autotune-runs.kubeconfig`, holding only that account's token. It checks
  that the token works and can't reach outside its namespace.

If the API server's name resolves only through this machine's `/etc/hosts`,
the kubeconfig gets its address instead, since the platform's pods can't
resolve it. `--api-server https://<address>:6443` sets the address yourself.
`deploy/gpu-cluster.sh remove` revokes the account.

## 3. Add it on Resources

In LLM AutoTune, open **Resources ▸ Add GPU cluster**:

1. Upload the kubeconfig and press **Connect**. The platform lists the
   cluster's GPU nodes, with their cards.
2. Tick the nodes to use and press **Register**. Each becomes a machine pinned
   to its node.
3. On each machine, press **Lease to platform**. Campaigns only use leased
   machines; **End lease** gives one back.

Image pull secrets and extra tolerations for the cluster are under
**Advanced**. GPU taints are tolerated on their own. A cluster's **Nodes**
button registers nodes added later.

## 4. Search policies

A campaign without a policy tries every config in its search space. A policy
decides what to try next instead. The install registers one, `random-search`,
from its published image
(`ghcr.io/modelsphere/llm-autotune-policy-random-search`); pick it on a
campaign's **Search** step. Its pod runs on the GPU cluster, so those nodes pull
it from ghcr.io. If they can't, copy the image into a registry they reach and
change it on the **Policies** page, or build it from source:

```bash
deploy/quickstart.sh policy policies/random-search --registry registry.example.com/team
```

That builds `policies/random-search` (the
[llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies)
submodule, fetched if missing), pushes it and points the `random-search`
registration at it. The script remembers the registry. Writing your own:
[after installing](after-installing.md#search-policies).

## 5. Run a first campaign

1. **Search spaces ▸ New search space**: the engine settings to try. Start small, a few
   values of one or two settings.
2. **Campaigns ▸ New campaign**, step by step:
   - **Model:** engine, image, and the model path on the nodes.
   - **Machines:** the leased machines to use.
   - **Search:** the search space, and a policy or none.
   - **Benchmark:** synthetic prompts (input and output tokens, concurrency
     levels, requests per slot) or a replay, and the objective.
   - **Schedule:** a nightly window, or none.
   - **Check:** checks the machines before anything starts. On a cluster, a
     probe pod on each node pulls the engine and policy images and mounts the
     model path, so a wrong tag or path shows up here and not at the first run.
3. **Create campaign**, then on its page **Start** (or **Run now**, outside a
   schedule). A policy campaign shows its policy's progress on the same page;
   **Pause** lets current runs finish, **Stop** ends them.

## Trying it all on a laptop

The same steps work with two [kind](https://kind.sigs.k8s.io/) clusters on one
machine: one for the platforms, and one made to look like a GPU cluster. Its
runs are the mock engine, so nothing it measures means anything; the point is
the install and the loop. Give Docker about 8 GB.

```bash
kind create cluster --name autotune                    # the platform cluster
deploy/quickstart.sh --context kind-autotune
deploy/dev/mock-gpu-cluster.sh                         # kind-autotune-gpu: 4 mock GPUs, the mock engine
deploy/gpu-cluster.sh --context kind-autotune-gpu      # writes llm-autotune-runs.kubeconfig
```

Then follow steps 3 to 5 at <http://localhost:8080>:

- **Resources ▸ Add GPU cluster:** upload `llm-autotune-runs.kubeconfig`.
  `gpu-cluster.sh` writes the cluster's address on the Docker network, which
  the platform's pods reach, rather than its localhost port.
- **The campaign:** engine `sglang`, the image and model path that
  `mock-gpu-cluster.sh` prints, and a search space over the mock's own knobs,
  such as `mock_token_ms: [2, 20]`. Keep the benchmark small (128 input and 32
  output tokens, concurrency `1, 4`, 5 requests per slot): the laptop runs the
  benchmark client too.

If you use a local proxy app (`HTTPS_PROXY=http://127.0.0.1:…`), create the
platform cluster without it, `env -u HTTPS_PROXY -u https_proxy kind create
cluster --name autotune`: kind hands the proxy to its nodes, where 127.0.0.1 is
the node itself, and every image pull fails. `mock-gpu-cluster.sh` and
`deploy/demo.sh --kind` leave such a proxy out on their own.

`deploy/quickstart.sh down`, `deploy/dev/mock-gpu-cluster.sh down` and
`kind delete cluster --name autotune` remove it all.

## Upgrading and uninstalling

- Run `deploy/quickstart.sh` again to apply changes, or after pulling a newer
  release. It upgrades both releases in place and keeps their data.
- Your own values go in `.quickstart/llm-autotune.custom.yaml` and
  `.quickstart/llm-bench.custom.yaml`, applied last.
- `deploy/quickstart.sh down` removes both releases and their data.

## A cluster that cannot reach Docker Hub

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

The images keep their tags (`4pdosc/llm-autotune-backend:<version>` and so
on), so mirror the versions the charts name. The search policy comes from
ghcr.io; mirror it too and change its image on the **Policies** page. If GitHub isn't reachable
either, check out llm-bench at the matching tag and pass
`LLMBENCH_CHART=<clone>/deploy/helm/llm-bench`.

## Bare-metal machines over ssh

The worker ssh'es into a box and runs the engine with `docker run`; see
[after installing](after-installing.md#bare-metal-machines-over-ssh).

## When something goes wrong

- **A pod stuck during the install:** the script prints why. `ImagePullBackOff`
  is a registry the nodes can't reach (see above), or on kind a proxy on
  127.0.0.1 ([above](#trying-it-all-on-a-laptop)); `Pending` is usually a
  missing default StorageClass.
- **Connect fails when adding a GPU cluster:** the message says whether the
  name didn't resolve or the server didn't answer from the platform's pods.
  Re-run `gpu-cluster.sh` with `--api-server`.
- **A run never becomes ready:** the run's page and its log say why (an image
  pull, a missing model path, no free cards). **Check** in New campaign finds
  most of these before a campaign starts.
- **A policy campaign sits in starting:** its panel on the campaign page shows
  what the policy pod is waiting on.
