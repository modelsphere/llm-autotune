# Ascend NPU clusters

The Kubernetes driver can request integer accelerator resources reported by a
device plugin. NVIDIA remains the default. Existing `gpu_*` API fields also
represent NPUs; their counts must represent the same allocation unit throughout
a campaign. Shared or sliced resources are not automatically equivalent to
physical cards, and multidimensional requests (memory, cores, device count) are
not supported by the single-resource configuration.

## Upgrade

Run `cd backend && uv run alembic upgrade head` before starting the updated API
and workers. Migration `019_cluster_gpu_product_label` adds the cluster's
product-label setting and preserves NVIDIA defaults for existing rows.

## Configure a cluster

In Resources, add or edit the cluster and set:

| Field | Ascend example |
| --- | --- |
| Namespace | `llm-autotune-runs` (or the kubeconfig's namespace) |
| Workload kind | `deployment` |
| GPU / NPU resource | The device plugin's confirmed whole-device resource, e.g. `huawei.com/npu` |
| GPU / NPU product label | `node.kubernetes.io/npu.chip.name` |
| RuntimeClass | The cluster's actual Ascend RuntimeClass, or empty when the default runtime handles NPUs |

`huawei.com/npu` is an example, not a universal Ascend resource name. Inspect
`kubectl get nodes -o json` and confirm the allocation semantics with the cluster
administrator. Do not add together alternate whole-device, shared, or sliced
resources exposed for the same devices.

For the default cluster configured through environment variables, use
`AUTOTUNE_K8S_GPU_RESOURCE`, `AUTOTUNE_K8S_GPU_PRODUCT_LABEL`, and
`AUTOTUNE_K8S_RUNTIME_CLASS`. Registered clusters use their own stored settings.

Register nodes or pools after configuring the resource and product label.
The exact chip label `910B4-1` normalizes to `ASCEND-910B4-1`. Other models must
be added to `app.hardware.GPU_TYPES` with explicit aliases as necessary; avoid
merging distinct chip variants into one comparison group.

Use an Ascend-compatible engine image with matching CANN/TorchNPU dependencies
and engine arguments. The device plugin/runtime must inject the assigned devices
and driver libraries. If the installation requires explicit extra host mounts,
use deployment mode and configure the workload's volumes. Custom mode only
supports the model mount; it now forwards RuntimeClass explicitly, including an
empty value that disables the operator's NVIDIA fallback.

Capacity, pending-placement diagnostics, preflight and run provenance use the
cluster's configured resource/label. Free capacity is unknown (`null`) when the
credential cannot list pods across namespaces. Image/model preflight verifies
image pulls and weights access; it does not verify NPU initialization. Validate
an actual one-card engine run and `/v1/models` readiness before a campaign.
