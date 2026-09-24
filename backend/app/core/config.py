from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOTUNE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://autotune:autotune@127.0.0.1:28432/autotune"
    sync_database_url: str = "postgresql+psycopg2://autotune:autotune@127.0.0.1:28432/autotune"

    # The first admin, created by the bootstrap when the users table is empty.
    # No default password on purpose: with none set, no user is seeded and the
    # bootstrap says so, rather than shipping a known login.
    admin_username: str = "admin"
    admin_password: str = ""

    jwt_secret: str = "dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    llmbench_base_url: str = "http://llmbench:8000"
    # The API key of LLMBench's service account ("llmb_…", seeded by LLMBench's
    # deployment from the same value) — authenticates directly, no login call.
    # Email/password remain as a fallback when no key is configured.
    llmbench_api_key: str = ""
    llmbench_email: str = ""
    llmbench_password: str = ""
    # The benchmark a campaign screens with when it names none: AutoTune's own,
    # a sweep-only benchmark it creates and locks on LLMBench from
    # app/evaluation/benchmark_templates/ (at bootstrap, or via the UI).
    llmbench_benchmark_slug: str = "autotune-screen-v1"
    llmbench_ensure_benchmarks: bool = True
    # Where a PERSON opens LLMBench in a browser, for links in reports and the
    # UI. Distinct from llmbench_base_url, which is how this platform's pods
    # reach it (inside a cluster, a service address no browser can open).
    # Empty = derive from llmbench_base_url by dropping a trailing /api.
    llmbench_web_url: str = ""

    # The window a drafted campaign runs in when its author names none
    # (POST /campaigns/draft). Empty = no clock: the draft starts when started.
    default_daily_start: str = ""
    default_daily_end: str = ""
    default_schedule_timezone: str = ""
    # Run LLMBench's own endpoint probe before submitting (near-free, and it
    # tests reachability from the benchmark platform's network position).
    llmbench_preflight: bool = True
    # Ride out a flaky intranet: every LLMBench call retries transient faults
    # (connect/read timeouts, resets, DNS failures, and 502/504 from a gateway
    # in front of it) before giving up. Generous by design — a blip should never
    # fail an otherwise-good run. Backoff is exponential: 2s, 4s, 8s, 16s, capped
    # at 20s, so five attempts wait ~30s in total before conceding.
    llmbench_max_attempts: int = 5
    llmbench_retry_backoff_seconds: float = 2.0
    llmbench_retry_backoff_cap_seconds: float = 20.0

    # Browser origins allowed to call this API, comma-separated. "*" (the
    # default) is right when the SPA is served from this same origin, which is
    # what the chart and the dev stack both do. Name origins explicitly when the
    # UI is hosted somewhere else.
    cors_origins: str = "*"

    # If set and the directory exists, the API also serves the built frontend
    # (dev-box convenience — one port for UI + API, no nginx needed).
    static_dir: str = ""

    # Where a person's BROWSER reaches this platform — the UI's own address
    # (its nginx, port 28080 on the deploy stack), NOT `public_api_url`, which
    # is the API container as GPU machines see it and serves no page of the
    # app. Used to build links we hand to other systems: a run's page, carried
    # through to the benchmark platform's submission list so a row there can
    # point back at what produced it. Empty means no link is sent.
    public_ui_url: str = ""

    # Let the worker take production down after a PASSING canary and put it
    # back when the window closes. A night is unattended by definition; set
    # false to require a human on the Resources page for both steps.
    auto_baseline_lifecycle: bool = True

    # Whether the worker RESTARTS production itself when a window or lease ends.
    # Off by default: on these fleets an admin owns production and restores it
    # by hand (often they never handed it to us running in the first place —
    # they stopped it themselves). Auto-restart is the one lifecycle step that
    # relaunches a service we did not start, so it stays opt-in; when off, the
    # machine is handed back with production left exactly as we found it and an
    # event flags that a manual restore is owed. Capture and the hand-over clear
    # still run under auto_baseline_lifecycle — only the put-back is gated here.
    auto_restore_production: bool = False

    # Below this, a difference from the baseline is not distinguishable from
    # run-to-run variance. Measured on node-24 with the long benchmark: four runs
    # of the same config spanned 0.24%, so 1% is ~4x the observed noise.
    # Raise it for shorter/noisier benchmarks.
    report_noise_threshold_pct: float = 1.0

    worker_tick_seconds: int = 10
    run_log_dir: str = "./runs"
    default_max_run_minutes: int = 150

    # -- policy sessions (docs/api/policy-contract.md) ------------------------
    #
    # The URL a policy container on a GPU host uses to reach THIS platform —
    # the one thing the platform cannot infer about itself. Injected into every
    # policy container as AUTOTUNE_API_URL; the campaign preflight curls
    # <public_api_url>/api/health from the machine to prove the path exists
    # before a night depends on it. Empty = policy campaigns refuse to start.
    public_api_url: str = ""
    policy_heartbeat_interval_seconds: int = 30
    policy_heartbeat_timeout_seconds: int = 180
    # A wedged policy (silent but container alive) is failed at 2x the timeout;
    # a dead container is failed as soon as the driver reports it gone.
    policy_finalize_grace_seconds: int = 300
    # Productivity watchdog default when a campaign does not set its own
    # search_idle_timeout_s: how long a SEARCHING session may sit with nothing
    # running and no delegated request before earning an idle strike.
    policy_search_idle_seconds: int = 300
    # Session tokens outlive their session by this grace, then die; hard cap
    # regardless of session length.
    policy_token_grace_hours: int = 1
    policy_token_max_hours: int = 48
    policy_state_max_bytes: int = 16 * 1024 * 1024
    # Two clocks for a run that is coming up, because pulling a cold multi-GB
    # engine image is not the same failure as a wedged model load:
    #
    #   ready_timeout_minutes — once the engine container is actually RUNNING,
    #     how long it may take to answer /v1/models before it is called wedged.
    #     Big models load for 5–15 min (per the prod deploy scripts).
    #
    #   image_pull_timeout_minutes — while the pod is still scheduling or pulling
    #     its image (no container running yet), the more generous budget. A first
    #     pull of a ~20GB CUDA image onto a cold node genuinely takes many
    #     minutes, and a pod making that progress is not wedged. Kept separate so
    #     a slow pull does not eat the model-load budget and trip a false
    #     ready_timeout (which is exactly what bit a k8s node that had never
    #     cached the image). Should be >= ready_timeout_minutes.
    #
    # Substrates that cannot distinguish the two phases (ssh_docker) fall back to
    # the single ready_timeout, unchanged.
    ready_timeout_minutes: int = 30
    image_pull_timeout_minutes: int = 45
    # Tearing a run's container down is graceful-then-forceful, verified by the
    # janitor rather than fire-and-forget: a SIGTERM first (let the engine
    # release GPU memory and NCCL cleanly), then this many seconds later a
    # forced removal if it is still up, and the machine's cards stay reserved
    # until the container is confirmed gone. If it will not die within
    # `teardown_giveup_seconds`, the machine is quarantined for a human rather
    # than handed to the next run on top of a container that never left.
    teardown_grace_seconds: int = 60
    teardown_giveup_seconds: int = 600
    # Once /v1/models answers, the health gate posts ONE chat completion and
    # waits this long for it. The number is dominated by the cold FIRST request,
    # not steady state: a big reasoning model (Kimi-K3, 2.8T) answers the probe
    # only after one-time init on its first forward (flashinfer JIT, kernel
    # autotune, the DSpark draft's first use, decode-graph capture) plus up to
    # 512 reasoning tokens — which blew past the old hardcoded 60s and failed
    # every candidate at `health_check`. Generous on purpose: a genuinely wedged
    # model is already caught earlier by ready_timeout at the /v1/models stage,
    # and the whole run is still bounded by max_run_minutes.
    health_probe_timeout_seconds: float = 600.0

    ssh_connect_timeout: int = 15
    # Kill switch only. GPU flags are normally decided per machine (a machine
    # with gpu_count=0 never gets them); set "none" to force them off fleetwide.
    docker_gpu_mode: str = "nvidia"

    # -- k8s launch driver (the GPU cluster substrate) -----------------------
    #
    # Set once the fleet's k8s migration lands. The driver renders a LaunchSpec
    # into a workload and polls it; it never touches nodes. Default mode is a
    # plain Deployment + NodePort Service; "custom" mode renders a TuningRun for
    # the autotune-operator (see ../../autotune_operator, validated on the
    # cluster). Cluster-specific values (namespace, node host, GPU resource) stay
    # configuration below.
    #
    # How the driver reaches the cluster:
    #   "client"      — the official Kubernetes Python client. The backend is
    #                   normally a REMOTE client of the GPU cluster (it may run
    #                   nowhere near it — a container on a bastion, or on a
    #                   separate platform cluster), so the norm is a SCOPED
    #                   kubeconfig pointing at the GPU cluster (k8s_kubeconfig).
    #                   In-cluster credentials apply ONLY when the backend itself
    #                   runs as a pod inside the GPU cluster (k8s_in_cluster).
    #   "kubectl"     — shells out to the kubectl binary (inherits the ambient
    #                   kubeconfig/service-account, mirrors the ssh driver's use
    #                   of the ssh binary). Handy where a client dep is unwanted.
    #   "unavailable" — the default: every call raises a clear "k8s not configured"
    #                   error rather than a cryptic one.
    k8s_api_mode: str = "unavailable"
    k8s_kubectl_bin: str = "kubectl"
    k8s_namespace: str = "llm-autotune"
    # kubeconfig context to target; empty = the current context.
    k8s_context: str = ""
    # Path to a SCOPED kubeconfig for "client" mode — a ServiceAccount token with
    # only the tuningruns + pods permissions, whose server is the GPU cluster's
    # API. This is the normal path, because the backend usually runs OUTSIDE the
    # GPU cluster. Empty = use the ambient kubeconfig (KUBECONFIG / ~/.kube/config).
    # Never the admin kubeconfig.
    k8s_kubeconfig: str = ""
    # On an empty install with k8s_in_cluster set, register the cluster this
    # process runs in as one machine pool, so a fresh deployment has somewhere
    # to put its first run instead of an empty Resources page. Only ever when
    # NO machine exists; it never touches a fleet someone has described.
    auto_register_cluster: bool = True

    # Set true ONLY when the backend runs as a pod INSIDE the GPU cluster, to
    # authenticate with that pod's own ServiceAccount (load_incluster_config)
    # instead of a kubeconfig. Off by default — the backend is normally remote, so
    # in-cluster config would point at the WRONG cluster (whichever one holds the
    # pod), which is exactly the footgun to avoid.
    k8s_in_cluster: bool = False
    # Which object the driver renders for one run. The cluster design is still
    # TBD, so this is a switch, not a commitment:
    #   "deployment" — a plain Deployment + NodePort Service. The universal k8s
    #                  primitive: works on any cluster, no CRD/operator needed,
    #                  right for single-node. This is the concrete, non-speculative
    #                  path we can run today.
    #   "custom"     — a TuningRun the autotune-operator reconciles (fields below).
    #                  Set this once the operator is deployed on the cluster.
    k8s_workload_kind: str = "deployment"
    # The custom resource the operator reconciles (only used when
    # k8s_workload_kind="custom"). Defaults address the autotune-operator's
    # TuningRun CRD; override only if its group/kind changes. apiVersion is
    # "{group}/{version}"; `plural` is the API-path name.
    k8s_cr_group: str = "tuning.modelsphere.dev"
    k8s_cr_version: str = "v1alpha1"
    k8s_cr_kind: str = "TuningRun"
    k8s_cr_plural: str = "tuningruns"
    # The label the operator puts on the pods it creates for a TuningRun. In
    # custom mode the driver does not create the pods (the operator does), so
    # logs/environment/exit-info select on this label; the value is the run's
    # object name (autotune-run-<id>).
    k8s_cr_pod_label: str = "tuning.modelsphere.dev/run"
    # Optional TTL written into the TuningRun (custom mode): a safety net so the
    # operator tears a run down after this many seconds if the platform crashes
    # and never deletes it. 0 = no TTL (the platform owns teardown).
    k8s_run_ttl_seconds: int = 0
    # How the served endpoint is reached in "deployment" mode: a NodePort Service.
    # `k8s_node_host` is the host the endpoint URL is built from (a node IP/DNS
    # LLMBench can reach); empty = derive from a node's InternalIP.
    # `k8s_service_nodeport` pins the NodePort (0 = let k8s allocate one).
    k8s_node_host: str = ""
    k8s_service_nodeport: int = 0
    # The scheduler's resource name for a GPU, requested per pod. Placement
    # (which node, which devices) is the cluster's job, so the spec asks for a
    # count and never names an index.
    k8s_gpu_resource: str = "nvidia.com/gpu"
    # The RuntimeClass a GPU pod asks for. NVIDIA's device plugin requires the
    # nvidia runtime to be selected; ours is named `nvidia`, but a cluster that
    # names it differently (or has none) would reject the pod outright, so this
    # is a setting rather than the hardcoded string it used to be.
    k8s_runtime_class: str = "nvidia"
    # Engine container requests/limits. Historically the engine asked for ONLY
    # `limits: nvidia.com/gpu`; a namespace with a LimitRange or quota that
    # demands cpu/memory requests rejects such a pod outright, and the B300
    # cluster's deployment standard mandates them. Empty = omitted, so the
    # default keeps today's manifests byte-identical.
    k8s_engine_cpu_request: str = ""
    k8s_engine_memory_request: str = ""
    k8s_engine_cpu_limit: str = ""
    k8s_engine_memory_limit: str = ""
    # Constrain which nodes a run's pod may land on, as comma-separated
    # `label=value` pairs (e.g. "kubernetes.io/hostname=gpu-a100-1" or
    # "nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB"). Empty = the scheduler is
    # free. This matters when weights are a per-node hostPath that only some
    # nodes carry: without it the scheduler can place the pod on a node missing
    # the model, and the mount fails. Applies in both workload modes.
    k8s_node_selector: str = ""
    # GPU nodes are commonly tainted so that pods with no use for cards keep
    # off them — ours carry `nvidia.com/gpu=...:NoSchedule`, and every GPU
    # workload on the cluster (production serving included) tolerates it. A pod
    # that REQUESTS cards is the workload such a taint protects the node for,
    # so a GPU pod tolerates that key by default; without it the pod sits
    # Pending with "untolerated taint" and no amount of free GPUs helps.
    # Set false only for a cluster that uses the same key to mean something
    # narrower.
    k8s_tolerate_gpu_taint: bool = True
    # Further taints these pods may tolerate, comma-separated, each
    # `key=value:Effect`, `key:Effect` or `key` (exists, any effect) — e.g.
    # "dedicated=ml:NoSchedule". NOTE: only the deployment workload mode can
    # carry tolerations; the TuningRun CRD has no field for them, so in custom
    # mode they are dropped by the API server and the pod will not schedule.
    k8s_tolerations: str = ""
    # Names of imagePullSecrets (comma-separated) attached to every pod we
    # create — the engine, the policy Job, and the TuningRun the operator
    # renders. Empty on a cluster whose nodes already carry the registry
    # credential (ours do, which is why this went unnoticed for so long); a
    # guest cluster that does not will fail every launch with ImagePullBackOff.
    k8s_image_pull_secrets: str = ""
    # Where model weights come from. Empty = a per-node hostPath at the model
    # path the campaign names, which is how many clusters serve weights, and
    # is why a run must be pinned to nodes that carry the model. Set
    # `k8s_model_pvc` to a ReadOnlyMany/ReadWriteMany claim instead and the
    # weights stop being a property of the node: any node the scheduler picks
    # can mount them. `k8s_model_pvc_root` is the host path the claim's ROOT
    # corresponds to (e.g. /mnt/disk0/models), used to turn the campaign's
    # absolute model path into a subPath inside the volume — so one claim serves
    # every model and campaigns keep naming paths the way they always have.
    k8s_model_pvc: str = ""
    k8s_model_pvc_root: str = ""
    # Shared memory for the engine container. The ssh driver runs `docker run
    # --ipc=host` so NCCL/multi-GPU has the host's /dev/shm; the *safe* k8s
    # substitute is a sized in-memory emptyDir mounted at /dev/shm — bounded, and
    # WITHOUT sharing the node's IPC namespace the way hostIPC:true would. Applied
    # to GPU pods (custom mode sets the TuningRun's sharedMemoryMB; deployment mode
    # adds the volume). 0 disables it. MiB.
    k8s_shm_size_mb: int = 2048
    # Seconds a launch/get/delete kubectl call may take before it is treated as
    # a substrate failure (classified, not an escaped crash).
    k8s_call_timeout: int = 60

    # -- policy containers on the cluster ------------------------------------
    # A policy is a CONTROLLER, not a service: it dials the platform API, asks
    # for engine launches and reads results back. On k8s it is therefore a
    # `batch/v1` Job (run to completion, no restart — what `docker run -d`
    # without --restart already means), never a Deployment or a TuningRun.
    #
    # Modest requests, not BestEffort: a pod with no requests is first to be
    # evicted under node pressure, and losing the controller mid-night ends the
    # session. These are the whole ask for a delegated-only policy — it requests
    # no GPUs at all, so it can land on a CPU node.
    k8s_policy_cpu_request: str = "100m"
    k8s_policy_memory_request: str = "256Mi"
    # How long a finished policy Job (and its pod) survives before k8s reaps it.
    # Generous on purpose: the platform reads a dead policy's logs AFTER it
    # exits, and a reaped pod takes the only copy of them with it. This is a
    # leak guard, not a cleanup schedule — teardown deletes the Job itself.
    k8s_policy_ttl_after_finished: int = 86400
    # Hard kill for a policy pod, 0 = none (the default). The platform's own
    # clocks — lease drain, window cutoff, idle strikes — end a session
    # gracefully and give it time to finalize; this one just kills the pod
    # mid-sentence, so if it is set at all it must be far beyond any real
    # session and never the thing that fires first.
    k8s_policy_deadline_seconds: int = 0
    # Optional PriorityClass for policy pods. A controller costing 100m CPU
    # being preempted by production is a bad trade; empty = cluster default.
    k8s_policy_priority_class: str = ""

    # -- promotion (rolling a winner into the deploy of record) --------------
    #
    # A campaign's winner is handed to a PromotionTarget that opens a rollout:
    #   "manual" — the default. Renders the exact config for a human or a gitops
    #              pipeline to apply, and calls nothing.
    #   "gitlab" — opens a merge request against a Helm values file in a GitLab
    #              deploy repo, under the baseline's ownership policy.
    # Adding a target (a GitHub pull request, a catalog entry, an API call) is a
    # class in control/promotion/ plus a line in its registry; nothing above
    # that package names a concrete one.
    promotion_target: str = "manual"
    # Default ON: the target builds the whole draft — branch, diff, description
    # — records it, and writes nothing. Turn it off once a preview has been read.
    promotion_dry_run: bool = True

    # One platform-level token: the merge request is authored by the platform's
    # account and names the requesting user in its description. It needs to push
    # branches and open merge requests, nothing more.
    #
    # Which repo, branch and file a baseline mirrors is per baseline (its
    # DeployBinding); the settings here are only the defaults offered at binding.
    gitlab_base_url: str = ""          # e.g. https://gitlab.example.com
    gitlab_token: str = ""
    gitlab_project: str = ""           # path or numeric id of the deploy project
    # What the request branch is called: this prefix plus `<model>-run<id>-<stamp>`.
    # A prefix rather than a fixed name because a project may enforce a
    # branch-name push rule, which would reject the push outright and leave no
    # request at all. Keep a marker in it so the branch is identifiable as ours.
    gitlab_branch_prefix: str = "autotune/"
    # Trigger a pipeline on the MR branch after opening it.
    gitlab_trigger_pipeline: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
