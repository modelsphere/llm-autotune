[User guide](README.md) › **Troubleshooting**

# Troubleshooting

Symptoms and their likely causes. Problems during the install itself are in
[Installing](../install.md#troubleshooting).

## Signing in

**The admin password is lost.** The install stores it in
`.quickstart/secrets.env` (`AUTOTUNE_ADMIN_PASSWORD`), in the directory where
`deploy/quickstart.sh` ran. `deploy/quickstart.sh ui` prints the addresses and
logins again.

**There is no Create account link.** Sign-up is turned off on this install; an
admin adds users on the **Account** page.

## Adding a GPU cluster

**Connect fails.** The error states which part failed, as seen from the
platform's pods (not from the workstation):

- *the name did not resolve*: the kubeconfig names the API server by a name
  only the workstation can resolve (from `/etc/hosts`, for example). Run
  `deploy/gpu-cluster.sh --api-server https://<address>:6443` to write the
  address instead.
- *the server did not answer*: the platform's pods cannot reach that address.
  Check the network path, or set a reachable address with `--api-server`.
- anything else: the message comes from the cluster. Running
  `deploy/gpu-cluster.sh` again writes a fresh kubeconfig to upload.

**No GPU nodes are listed.** The nodes do not report `nvidia.com/gpu`, so the
NVIDIA device plugin is not running on them. `kubectl describe node <node>`
should show `nvidia.com/gpu` under *Capacity*. If the GPUs use another
resource name, set **GPU resource** under **Advanced**.

## Machines

**Add machine is refused: "could not read … GPUs".** The platform could not
read the GPU count over ssh (or the node selector matched no node). Fix the
access, or enter the **GPU count** under **Advanced** and save again.

**Smoke test fails at *endpoint*.** Runs are reached on a node's address and a
NodePort. The platform's pods and LLMBench's must both reach the GPU nodes'
NodePorts. If the nodes have several addresses, set the right one as
**Endpoint host** under **Clusters ▸ More ▸ Edit**.

**Smoke test warns about *card type*.** The platform did not recognize the GPU
model from the node's labels. Set it with **More ▸ Edit ▸ GPU type**; a
baseline is compared only with results on the same GPU type.

**A machine stays *handing back*.** Its runs are finishing, as **End lease**
requested. **More ▸ End lease now** stops them immediately.

**A machine cannot be edited or removed.** An unfinished campaign uses it. On
that campaign's page, use **⋯ ▸ Mark as done** (or **⋯ ▸ Delete** if it never
ran). A machine in a node group must be taken out of the group first.

## The Check step

| check | fails because | fix |
|---|---|---|
| **Container image** | the tag does not exist, the nodes cannot reach the registry, or it needs credentials | correct the image; mirror it to a reachable registry; add **Image pull secrets** to the cluster |
| **Model path** | the folder is not on that node | copy the weights there, or pin the campaign to machines that have them |
| **GPUs** | a candidate needs more GPUs than the machine has | remove the wide values from the search space, or use a larger machine |
| **Policy image** | the GPU nodes cannot pull the policy image | change its image on **Policies** to a reachable registry ([Search policies](policies.md#registering-a-policy)) |
| **Engine port** (ssh) | something already listens on the port | change **First engine port** under **Machines ▸ Advanced** |

A warning (yellow) does not stop the campaign. "Still being pulled" means the
probe stopped waiting for a large image; the first run waits for it instead.

## Campaigns

**It stays *scheduled*.** Its window has not opened yet; the schedule line
shows when it will. **Run now** starts it immediately.

**It is *active*, but nothing runs.** A banner on its page gives the reason for
each machine. Common reasons:

- none of its machines is leased (**Resources ▸ Lease to platform**);
- the window is too short for another run to finish before it closes;
- the GPUs are in use by other campaigns, or by workloads outside the
  platform;
- a pinned machine was removed.

**A policy campaign stays in *starting*.** The policy pod has not started on
the GPU cluster. The warning line under the policy strip gives the reason,
usually an image the nodes cannot pull or no node with room for it.

**A policy stays in *searching* with nothing running.** Its latest message
says what it is waiting for, such as "no free cards yet, will retry" while a
previous engine shuts down. **Logs** on the strip shows the policy's log.

**A log is empty.** A container that printed nothing, or was stopped before it
started, leaves no log, and the log says which. **Logs ▸ Activity** on the
campaign shows what the platform did either way: where each run was placed,
who stopped it, and what the policy asked for.

**The leaderboard stays empty.** No run has succeeded yet. Check the **Runs**
tab: if every run fails, the cause is shared (below).

## Datasets

**"LLMBench has no dataset collector".** LLMBench was installed without
one. Run the install again with `VICTORIALOGS_URL`
([After installing](../after-installing.md#replay-datasets)).

**The model list is empty, or "Could not read the log store".** LLMBench
cannot reach the VictoriaLogs address from its pods, or the store has no
requests from the last day. Check the address under **Advanced**: unless the
install set one, it is `http://victorialogs.logging:9428`, where the
bodylog-victorialogs chart serves it from the `logging` namespace.

**No request matches.** The model is matched exactly, as clients send it,
and a node must be one that served that model. Clear **Nodes** to check
whether the model alone matches.

**A sample fails: "only N records collected, below the profile's minimum".**
The window held fewer matching requests than **Fewest requests**. Widen the
window, clear a node, or lower the minimum under **Advanced**. The previous
sample stays in use.

**"LLMBench did not let this platform create the dataset".** The installed
LLMBench is older than this AutoTune and does not let its service account
create datasets. Upgrade LLMBench.

**No new sample is taken.** A campaign is measuring on the current sample;
the list shows *waits for* and the campaign. The sample is taken when that
campaign finishes, or now with **Collect now**.

## Runs fail

Open the failed run and read **Error** and **Captured log**.
[Runs and failures](runs.md#kinds-of-failure) lists every failure kind.

**Every run fails the same way.** The cause is not the configs: usually a
wrong image or model path, an engine that does not support the model, or a
flag the image does not recognize. Fix it, then press **Re-run *N* failed
configs**.

**`oom` on some configs.** They need more memory than the GPUs have, which is
a valid result. Narrow the space (a lower `mem_fraction_static`, fewer
`max_running_requests`, a larger `tp`) if they were expected to fit.

**`ready_timeout`.** The engine did not respond within the campaign's model
load limit (2 hours by default) of starting. A model that needs longer to load
needs a higher **Model load limit**, under **1 Model → Advanced** when the
campaign is created. Otherwise the log usually shows the engine stalled or
crashed.

**`bench_preflight`.** LLMBench could not reach the engine. LLMBench's pods
need to reach the GPU nodes' NodePorts, like the platform's.

**`image_pull` or `mount_failed` on one machine only.** That node lacks the
image access or the weights. The run is retried elsewhere; pin the campaign to
the machines that work.

## Local kind setup

See [Local setup with kind](../install.md#local-setup-with-kind). The most
common problem is a local proxy: kind passes `HTTPS_PROXY=127.0.0.1:…` to its
nodes, where it points at nothing, and every image pull fails. The mock
engine's numbers are not performance data, so rankings on a local setup are
mostly noise.

---

← [Account, users and API keys](account.md) · [User guide](README.md)
