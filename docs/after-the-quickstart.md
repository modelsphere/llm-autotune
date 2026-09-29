# After the quickstart

[`deploy/quickstart.sh`](../deploy/quickstart.sh) leaves you with LLMBench and
LLM AutoTune on one cluster, connected, with the mock engine and a demo
campaign. This page takes that install further one piece at a time, without
starting over: every step is a value you set, or a command run against the
same install.

## Changing a setting

The script keeps two values files of yours and applies them last, after
everything it sets itself:

| file | release | every value is in |
|---|---|---|
| `.quickstart/llm-autotune.custom.yaml` | LLM AutoTune | [`deploy/helm/llm-autotune/values.yaml`](../deploy/helm/llm-autotune/values.yaml) |
| `.quickstart/llm-bench.custom.yaml` | LLMBench | LLMBench's [`values.yaml`](https://github.com/modelsphere/llm-bench/blob/main/deploy/helm/llm-bench/values.yaml) |

Edit one, then run `deploy/quickstart.sh` again. It upgrades both releases in
place and keeps their data. It does not recreate the demo campaign, and it
never overwrites these two files; `deploy/quickstart.sh down` keeps them too.

AutoTune settings the chart has no value for (promotion, timeouts, the default
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
- **On a cluster other than kind, minikube, k3d, Docker Desktop or OrbStack**,
  the cluster pulls images from a registry: add `--registry <repo>` (one you can
  push to and the nodes can pull from) the first time; the script remembers it.
- **Without the script**, register an image with the API, using a key from the
  **API Keys** page:

  ```bash
  curl -X POST http://localhost:8080/api/policies -H "X-API-Key: $KEY" \
    -H 'Content-Type: application/json' -d '{"name": "random-search",
      "image": "<registry>/llm-autotune-policy-random-search:0.1.0",
      "gpus_in_container": false, "needs_model": false}'
  ```

A policy runs as a Job next to the runs and calls the platform back at
`publicApiUrl`. On the quickstart that is the API's in-cluster address, which
works because runs land in the same cluster; when they land elsewhere (below),
set `publicApiUrl` to an address reachable from there.

## Real GPUs

The quickstart's only machine, `local-cluster`, is the cluster itself with no
cards. GPUs can come from three places, and one install can use all of them.

### GPU nodes in this cluster

```yaml
# .quickstart/llm-autotune.custom.yaml
gpuCluster:
  gpuResource: nvidia.com/gpu
  runtimeClass: nvidia          # "" if the cluster has no RuntimeClass by that name
  tolerateGpuTaint: true
  # nodeSelector: "nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3"
```

Run the script again, then on **Resources** use **Refresh capacity** on
`local-cluster`: it reads the card count and type from the nodes.

A run mounts its model weights from the node, as a hostPath at the campaign's
model path, so the nodes it can land on must hold the weights there (pin them
with `gpuCluster.nodeSelector`). To serve weights from one shared volume
instead, set `gpuCluster.modelPvc` to a ReadOnlyMany/ReadWriteMany claim and
`gpuCluster.modelPvcRoot` to the path the campaigns' model paths are relative
to. Engine images that need credentials take a pull secret through `extraEnv`
(`AUTOTUNE_K8S_IMAGE_PULL_SECRETS`).

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
ssh user. The machine needs Docker and the NVIDIA container toolkit. Such a box
is usually shared with production: the platform captures what runs there
before it takes the box and can put it back afterwards
([deploying.md](deploying.md#bare-metal-boxes-over-ssh)).

## Your own models

With a machine that has GPUs, a campaign serves your model with a real engine
image (sglang or vLLM) and your search space. The usual path starts from the
configuration production runs today: record it on **Baselines**, then **Tune
from this** drafts a campaign from it. [How it works](workflow.md) describes the
whole loop. Screen real engines with the default benchmark,
`autotune-screen-v1`; the quickstart's `autotune-quickstart-v1` is sized for
the mock.

## Reaching the UIs without port-forward

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

The demo needs none: LLMBench's throughput sweep generates its prompts. For
the academic suites, and for replaying captured or live gateway traffic, give
LLMBench a datasets volume in `.quickstart/llm-bench.custom.yaml`
(`datasets.enabled`) and follow LLMBench's
[deploying guide](https://github.com/modelsphere/llm-bench/blob/main/docs/deploying.md#storage)
and [replay datasets](https://github.com/modelsphere/llm-bench/blob/main/docs/replay-datasets.md).
The academic suites are fetched from Hugging Face; nothing else LLMBench runs
needs it.

## Promoting a winner

By default a campaign's winner is rendered as the exact configuration to apply,
for a person or a pipeline. To have it opened as a GitLab merge request against
the file production is deployed from:

```bash
kubectl -n llm-autotune create secret generic autotune-gitlab --from-literal=token=<token>
```

```yaml
# .quickstart/llm-autotune.custom.yaml
extraEnv:
  - {name: AUTOTUNE_PROMOTION_TARGET, value: gitlab}
  - {name: AUTOTUNE_GITLAB_BASE_URL, value: https://gitlab.example.com}
  - {name: AUTOTUNE_GITLAB_PROJECT, value: group/deploy-repo}
  - name: AUTOTUNE_GITLAB_TOKEN
    valueFrom: {secretKeyRef: {name: autotune-gitlab, key: token}}
  # - {name: AUTOTUNE_PROMOTION_DRY_RUN, value: "false"}   # once a preview reads right
```

Which file a baseline's winner is proposed against is set on the baseline, on
**Baselines**. Requests are dry runs until `AUTOTUNE_PROMOTION_DRY_RUN` is false.

## The operator

To have runs reconciled by the TuningRun operator instead of created as
Deployments, install it ([operator/INSTALL.md](../operator/INSTALL.md)) and set
`operator.enabled: true`.

## Starting over

`deploy/quickstart.sh down` removes both releases and their data (add `--kind`
to delete the kind cluster too); the next `deploy/quickstart.sh` installs from
scratch with new passwords, and still applies your `*.custom.yaml` files.
