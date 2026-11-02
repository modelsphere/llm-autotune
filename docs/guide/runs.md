[User guide](README.md) › **Runs and failures**

# Runs and failures

A **run** is one attempt at one candidate: the engine started with that
config, health-checked, benchmarked and stopped. **Runs** in the top menu
lists every run of every campaign, newest first. Filter it by status
(*running* is every live state), campaign or owner. Click a row to open the
run's details on its campaign's page.

![The Runs page](images/runs-page.png)

| column | means |
|---|---|
| **Campaign** | the campaign it belongs to |
| **Status** | where it is in its life (below) |
| **Failure** | for a failed run, what kind of failure (below) |
| **Owner** | who owns its campaign |
| **Container** | the name of its container or Kubernetes workload |
| **Started**, **Took** | when it started, and how long it took |

**Stop** on a live run ends that run: on the worker's next tick its engine is
removed and its GPUs released. The config is not tried again unless **Re-run
failed configs** is pressed on the campaign. (**Stop** on the campaign differs:
the configs it interrupts are queued again.)

## A run's life

| status | what is happening |
|---|---|
| **pending** | waiting for a machine with enough free GPUs, inside the window |
| **launching** | starting the engine: on Kubernetes, creating the pod and pulling the image |
| **waiting ready** | the engine is loading the model; the platform waits until it answers |
| **health check** | a short check that it answers requests correctly |
| **benching** | LLMBench is sending the workload |
| **serving** | (policy campaigns) the engine is up and held for the policy to use |
| **succeeded** | measured; its score is on the leaderboard |
| **failed** | something went wrong; **Failure** says what kind |
| **stopped** | stopped by a user, a lease ending or the window closing |

Model loading usually takes longest. A run that does not become ready within
the campaign's model load limit (2 hours unless the campaign sets another)
fails as `ready_timeout`.

## Kinds of failure

Some failures are caused by the config and repeat on every attempt; others
are caused by the machine, the network or the registry. The platform handles
them differently:

- **Config failures** are recorded as the config's result and not retried. A
  policy receives them and can avoid that region.
- **Infrastructure failures** are not held against the config. The candidate
  is retried on a new placement, up to three attempts.

| failure | cause | usually means |
|---|---|---|
| `oom` | config | out of GPU memory: lower `mem_fraction_static` or `max_running_requests`, or use more GPUs |
| `bad_config` | config | the engine refused a flag or value (`unrecognized arguments`, `invalid argument`) |
| `model_load` | config | the weights could not be loaded: wrong path, or not a model the engine supports |
| `ready_timeout` | config | the engine never became ready in time |
| `nccl` | config | GPUs could not talk to each other (multi-GPU or multi-node) |
| `segfault` | config | the engine crashed |
| `benchmark_not_passed` | config | the benchmark ran but the engine failed it |
| `image_missing`, `image_pull` | infrastructure | the image name or tag is wrong, or the nodes cannot reach the registry |
| `mount_failed` | infrastructure | the model path does not exist on that node |
| `unschedulable` | infrastructure | no node has free GPUs right now; the run waits |
| `unplaceable` | infrastructure | no node can ever take it, such as more GPUs than any node has |
| `port_conflict`, `gpu_busy` | infrastructure | something else held the port or a GPU at launch |
| `ssh_timeout` | infrastructure | an ssh machine did not answer |
| `killed_externally`, `gone`, `orphaned` | infrastructure | the container was removed outside the platform |
| `crash_loop` | neither | the engine keeps crashing at start with no recognized cause in the log; read the log |
| `health_check` | neither | it came up but answered wrongly |
| `bench_preflight` | neither | LLMBench could not reach the engine's endpoint |
| `terminated` | neither | stopped deliberately: a stop request or the window closing |
| queued again | neither | the campaign was stopped or paused while this run was going; its config runs again on the next start |
| `unknown` | neither | no known pattern in the log; read it |

## Diagnosing a failure

Open the run's details:

- **The top line**: its status, the kind of failure, how long it took, its
  GPUs, its endpoint and its LLMBench submission.
- **The error** gives the platform's explanation.
- **The score** it was ranked on, whether it held the redlines, and the
  metrics the objective names. Its swept parameters are below; the full
  config, the engine command, the launch command and every metric are folded
  away underneath.
- **Captured log** has the engine's output, ending where it failed (for an
  active run, the log so far). **Download** saves it. If the engine crashed
  and restarted, the log from before the restart comes first. A run with no
  log says why, such as a run stopped before its engine was launched; the
  campaign's **Logs ▸ Activity** shows what happened to it.

![A run's details](images/campaign-run-drawer.png)

A failure on every candidate is rarely caused by the configs; it is usually
the image, the model path or the machines, and the campaign's
[pre-flight checks](running-campaigns.md#pre-flight-checks)
usually identify it. Fix it, then press **Re-run *N* failed configs** on the
campaign. See also [Troubleshooting](troubleshooting.md).

---

← [Baselines](baselines.md) · [User guide](README.md) · Next: [Account, users and API keys](account.md) →
