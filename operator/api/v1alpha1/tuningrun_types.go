/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// TuningRunSpec is the *request*: the autotune platform writes it to ask the
// cluster to run one serving workload for one candidate config, and never
// mutates it afterwards (a run is immutable — a new config is a new TuningRun).
// The operator only reads it.
//
// The operator is deliberately engine-agnostic: it does not know sglang from
// vLLM. The full engine argument vector is built by the platform's own engine
// adapter (the same one the ssh/docker path uses) and passed through verbatim in
// Args, so engine knowledge lives in exactly one place.
type TuningRunSpec struct {
	// Image is the serving container image, ideally digest-pinned (e.g.
	// registry.example.com/...@sha256:...) so a rollout is reproducible.
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`

	// ImagePullPolicy for the engine container. Defaults to IfNotPresent, since
	// large engine images should not be re-pulled inside the run's clock.
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	// +kubebuilder:default=IfNotPresent
	// +optional
	ImagePullPolicy string `json:"imagePullPolicy,omitempty"`

	// ImagePullSecrets are names of secrets in the run's namespace used to pull
	// Image (e.g. a registry credential).
	// +optional
	ImagePullSecrets []string `json:"imagePullSecrets,omitempty"`

	// Command overrides the image entrypoint. Usually left empty so the image's
	// own entrypoint runs; set it when the platform wants an explicit argv[0].
	// +optional
	Command []string `json:"command,omitempty"`

	// Args is the full engine argument vector — model path, served-model-name,
	// host, port, and the tuned knobs. The operator passes it through untouched.
	// +optional
	Args []string `json:"args,omitempty"`

	// Port is the TCP port the engine serves HTTP on. It drives the container
	// port, the readiness probe, and the Service target port.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port"`

	// ReadinessPath is the HTTP path polled for readiness. Defaults to
	// /v1/models — the OpenAI-style endpoint sglang/vLLM expose only once weights
	// are loaded, which is exactly the "ready to serve" signal we want.
	// +kubebuilder:default=/v1/models
	// +optional
	ReadinessPath string `json:"readinessPath,omitempty"`

	// GPUCount is the number of GPUs to request as an extended resource. Devices
	// are chosen by the scheduler — we never pin indices. 0 runs CPU-only (used
	// by tests and the occasional CPU workload).
	// +kubebuilder:validation:Minimum=0
	GPUCount int32 `json:"gpuCount"`

	// GPUResourceName is the extended resource requested per GPU. Defaults to
	// nvidia.com/gpu.
	// +kubebuilder:default=nvidia.com/gpu
	// +optional
	GPUResourceName string `json:"gpuResourceName,omitempty"`

	// RuntimeClassName for the pod. When nil it defaults to "nvidia" if GPUCount
	// > 0 and empty otherwise. Set it explicitly (including to "") to override.
	// +optional
	RuntimeClassName *string `json:"runtimeClassName,omitempty"`

	// ModelHostPath, when set, is mounted at ModelMountPath from the node's
	// filesystem — matching how the cluster serves weights today (a hostPath such
	// as /mnt/disk0/models). Leave empty if the image already carries the weights.
	// +optional
	ModelHostPath string `json:"modelHostPath,omitempty"`

	// ModelPVC, when set (and ModelHostPath is empty), mounts a
	// PersistentVolumeClaim of this name at ModelMountPath instead of a hostPath.
	// +optional
	ModelPVC string `json:"modelPVC,omitempty"`

	// ModelSubPath is the path WITHIN the model volume to expose at
	// ModelMountPath. It is how one shared weights PVC serves many models: the
	// claim is mounted once and each run names its own directory
	// ("modelforge/release_260817"). Ignored for a hostPath, which already names
	// the directory it mounts.
	// +optional
	ModelSubPath string `json:"modelSubPath,omitempty"`

	// ModelMountPath is where the model volume is mounted. Defaults to /models.
	// +kubebuilder:default=/models
	// +optional
	ModelMountPath string `json:"modelMountPath,omitempty"`

	// SharedMemoryMB, when > 0, mounts an in-memory emptyDir of this size at
	// /dev/shm — tensor-parallel engines need far more than the 64Mi default.
	// +kubebuilder:validation:Minimum=0
	// +optional
	SharedMemoryMB int32 `json:"sharedMemoryMB,omitempty"`

	// Env are extra environment variables for the engine container.
	// +optional
	Env map[string]string `json:"env,omitempty"`

	// NodeSelector constrains which nodes the workload lands on — e.g. pick a card
	// type with nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB, which matters when
	// a result is specific to a GPU model.
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`

	// Tolerations the pod carries. A GPU node is commonly tainted so that pods
	// with no use for cards keep off it (nvidia.com/gpu=...:NoSchedule on our
	// clusters), and a pod that REQUESTS cards is exactly what such a node is
	// being kept for — so it must tolerate that taint or it sits Pending on an
	// idle pool, reporting only "untolerated taint". The platform decides which
	// taints a run may ignore; the operator passes them through untouched.
	// +optional
	Tolerations []corev1.Toleration `json:"tolerations,omitempty"`

	// ServiceType controls how the engine endpoint is exposed. NodePort (default)
	// publishes it on the node network for a benchmarker outside the cluster;
	// ClusterIP keeps it in-cluster.
	// +kubebuilder:validation:Enum=NodePort;ClusterIP
	// +kubebuilder:default=NodePort
	// +optional
	ServiceType string `json:"serviceType,omitempty"`

	// TTLSeconds is a safety net. If > 0 and the run has existed this long, the
	// operator tears the workload down and marks it Expired, so a platform crash
	// cannot leak GPUs indefinitely. 0 disables it (the platform deletes the run).
	// +kubebuilder:validation:Minimum=0
	// +optional
	TTLSeconds int32 `json:"ttlSeconds,omitempty"`
}

// TuningRunPhase is the coarse lifecycle state that drives the platform's search
// loop. The absence of the object means "gone".
// +kubebuilder:validation:Enum=Pending;Starting;Ready;Failed;Expired
type TuningRunPhase string

const (
	// PhasePending: the run is accepted; the workload exists but no pod yet.
	PhasePending TuningRunPhase = "Pending"
	// PhaseStarting: a pod exists and is scheduling / pulling / loading weights.
	PhaseStarting TuningRunPhase = "Starting"
	// PhaseReady: the engine answers its readiness probe; Endpoint is set.
	PhaseReady TuningRunPhase = "Ready"
	// PhaseFailed: the pod crashed, OOM'd, or the image is bad. See Reason.
	PhaseFailed TuningRunPhase = "Failed"
	// PhaseExpired: torn down by TTLSeconds.
	PhaseExpired TuningRunPhase = "Expired"
)

// TuningRunStatus is the *response*: the operator writes it, the platform reads
// it. Phase maps onto the platform's own launch state machine
// (Pending/Starting → Starting, Ready → Ready, Failed → Crashed, gone → Gone).
type TuningRunStatus struct {
	// Phase is the coarse state; see TuningRunPhase.
	// +optional
	Phase TuningRunPhase `json:"phase,omitempty"`

	// Endpoint is the base URL where the served model answers, reachable per
	// ServiceType (e.g. http://<nodeIP>:<nodePort>). Set once Ready.
	// +optional
	Endpoint string `json:"endpoint,omitempty"`

	// NodePort is the allocated node port when ServiceType is NodePort.
	// +optional
	NodePort int32 `json:"nodePort,omitempty"`

	// PodName and NodeName record where the workload landed, for logs and
	// debugging.
	// +optional
	PodName string `json:"podName,omitempty"`
	// +optional
	NodeName string `json:"nodeName,omitempty"`

	// Reason is a short machine-readable cause, most useful on Failed (e.g.
	// OOMKilled, ImagePullBackOff, CrashLoopBackOff, Unschedulable), so the
	// platform can classify a failure rather than seeing only "failed".
	// +optional
	Reason string `json:"reason,omitempty"`

	// Message is a human-readable detail.
	// +optional
	Message string `json:"message,omitempty"`

	// StartTime is when the workload was first created; ReadyTime when it first
	// became Ready.
	// +optional
	StartTime *metav1.Time `json:"startTime,omitempty"`
	// +optional
	ReadyTime *metav1.Time `json:"readyTime,omitempty"`

	// ObservedGeneration is the spec generation the operator last acted on.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Conditions carry the standard Ready condition (and Failed detail).
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=tr
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="GPUs",type=integer,JSONPath=`.spec.gpuCount`
// +kubebuilder:printcolumn:name="Endpoint",type=string,JSONPath=`.status.endpoint`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// TuningRun is one serving workload the autotune platform runs to benchmark a
// single candidate config. It is the contract between the platform (writes spec)
// and this operator (writes status).
type TuningRun struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is standard object metadata. The platform chooses a deterministic
	// name (e.g. autotune-run-<id>) so it can find the run again after a restart.
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired workload.
	// +required
	Spec TuningRunSpec `json:"spec"`

	// status is the observed state.
	// +optional
	Status TuningRunStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TuningRunList contains a list of TuningRun.
type TuningRunList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []TuningRun `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(s *runtime.Scheme) error {
		s.AddKnownTypes(SchemeGroupVersion, &TuningRun{}, &TuningRunList{})
		return nil
	})
}
