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

package controller

import (
	"context"
	"fmt"
	"sort"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	tuningv1alpha1 "github.com/modelsphere/llm-autotune/operator/api/v1alpha1"
)

const (
	// managerName labels the objects this operator owns and is the controller's field manager.
	managerName = "autotune-operator"
	// runNameLabel ties a Deployment/Service/Pod back to its TuningRun.
	runNameLabel = "tuning.modelsphere.dev/run"
	// engineContainerName is the single serving container in each workload pod.
	engineContainerName = "engine"
	// syncPeriod is how often a non-terminal run is re-observed even without a watch event.
	syncPeriod = 10 * time.Second

	defaultModelMountPath = "/models"
	defaultReadinessPath  = "/v1/models"
	defaultGPUResource    = "nvidia.com/gpu"
	defaultServiceType    = string(corev1.ServiceTypeNodePort)
	nvidiaRuntimeClass    = "nvidia"
	readyConditionType    = "Ready"
)

// terminalWaitingReasons are container "waiting" reasons we surface as Failed. CrashLoopBackOff is
// included so a run that keeps dying is reported rather than looking like it is still starting; the
// platform decides when to give up.
var terminalWaitingReasons = map[string]bool{
	"ImagePullBackOff":           true,
	"ErrImagePull":               true,
	"InvalidImageName":           true,
	"CreateContainerConfigError": true,
	"CreateContainerError":       true,
	"RunContainerError":          true,
	"CrashLoopBackOff":           true,
}

// TuningRunReconciler reconciles a TuningRun object.
type TuningRunReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=tuning.modelsphere.dev,resources=tuningruns,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=tuning.modelsphere.dev,resources=tuningruns/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tuning.modelsphere.dev,resources=tuningruns/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch

// Reconcile drives one TuningRun toward "a workload is running and reachable", and reports what it
// observes back into status. It is deliberately thin: it creates a Deployment + Service (owned by the
// run, so they are garbage-collected when the run is deleted), maps pod state onto a coarse phase,
// resolves the endpoint, and enforces a TTL. It does not benchmark, score, or tune — that stays in the
// platform.
func (r *TuningRunReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)

	var run tuningv1alpha1.TuningRun
	if err := r.Get(ctx, req.NamespacedName, &run); err != nil {
		// Not found: the run was deleted and its owned objects are GC'd via ownerReferences.
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Being deleted, or already terminal: nothing to do.
	if !run.DeletionTimestamp.IsZero() || run.Status.Phase == tuningv1alpha1.PhaseExpired {
		return ctrl.Result{}, nil
	}

	// TTL safety net: if the platform forgot about this run, free its GPUs.
	if run.Spec.TTLSeconds > 0 {
		if age := time.Since(run.CreationTimestamp.Time); age > time.Duration(run.Spec.TTLSeconds)*time.Second {
			logger.Info("TTL exceeded, tearing down", "age", age.Round(time.Second).String(), "ttlSeconds", run.Spec.TTLSeconds)
			if err := r.teardown(ctx, &run); err != nil {
				return ctrl.Result{}, err
			}
			desired := run.Status.DeepCopy()
			desired.Phase = tuningv1alpha1.PhaseExpired
			desired.Reason = "TTLExceeded"
			desired.Message = fmt.Sprintf("torn down after %ds", run.Spec.TTLSeconds)
			setReadyCondition(desired, run.Generation)
			return ctrl.Result{}, r.applyStatus(ctx, &run, desired)
		}
	}

	// Ensure the workload objects exist (idempotent; a run's spec is immutable once created).
	if err := r.ensureDeployment(ctx, &run); err != nil {
		return ctrl.Result{}, err
	}
	svc, err := r.ensureService(ctx, &run)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Observe and report.
	desired, err := r.observe(ctx, &run, svc)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := r.applyStatus(ctx, &run, desired); err != nil {
		return ctrl.Result{}, err
	}

	return ctrl.Result{RequeueAfter: syncPeriod}, nil
}

// ensureDeployment creates the run's Deployment if it does not exist. A TuningRun is immutable, so an
// existing Deployment is left as-is.
func (r *TuningRunReconciler) ensureDeployment(ctx context.Context, run *tuningv1alpha1.TuningRun) error {
	var existing appsv1.Deployment
	err := r.Get(ctx, types.NamespacedName{Name: run.Name, Namespace: run.Namespace}, &existing)
	if err == nil {
		return nil
	}
	if !apierrors.IsNotFound(err) {
		return err
	}
	dep := buildDeployment(run)
	if err := controllerutil.SetControllerReference(run, dep, r.Scheme); err != nil {
		return err
	}
	if err := r.Create(ctx, dep); err != nil && !apierrors.IsAlreadyExists(err) {
		return err
	}
	return nil
}

// ensureService creates the run's Service if it does not exist, and returns the live object (with its
// allocated NodePort populated).
func (r *TuningRunReconciler) ensureService(ctx context.Context, run *tuningv1alpha1.TuningRun) (*corev1.Service, error) {
	var existing corev1.Service
	err := r.Get(ctx, types.NamespacedName{Name: run.Name, Namespace: run.Namespace}, &existing)
	if err == nil {
		return &existing, nil
	}
	if !apierrors.IsNotFound(err) {
		return nil, err
	}
	svc := buildService(run)
	if err := controllerutil.SetControllerReference(run, svc, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, svc); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return &existing, r.Get(ctx, types.NamespacedName{Name: run.Name, Namespace: run.Namespace}, &existing)
		}
		return nil, err
	}
	return svc, nil
}

// teardown deletes the run's owned Deployment and Service by name (idempotent).
func (r *TuningRunReconciler) teardown(ctx context.Context, run *tuningv1alpha1.TuningRun) error {
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: run.Name, Namespace: run.Namespace}}
	if err := r.Delete(ctx, dep); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	svc := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: run.Name, Namespace: run.Namespace}}
	if err := r.Delete(ctx, svc); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// observe computes the desired status from the current pod and Service.
func (r *TuningRunReconciler) observe(ctx context.Context, run *tuningv1alpha1.TuningRun, svc *corev1.Service) (*tuningv1alpha1.TuningRunStatus, error) {
	desired := run.Status.DeepCopy()
	desired.ObservedGeneration = run.Generation
	if desired.StartTime == nil {
		now := metav1.Now()
		desired.StartTime = &now
	}

	pod, err := r.currentPod(ctx, run)
	if err != nil {
		return nil, err
	}

	if pod == nil {
		desired.Phase = tuningv1alpha1.PhasePending
		desired.Reason = ""
		desired.Message = "workload created, waiting for a pod"
		desired.PodName = ""
		desired.NodeName = ""
		setReadyCondition(desired, run.Generation)
		return desired, nil
	}

	desired.PodName = pod.Name
	desired.NodeName = pod.Spec.NodeName

	phase, reason, message := phaseFromPod(pod)
	desired.Phase = phase
	desired.Reason = reason
	desired.Message = message

	if phase == tuningv1alpha1.PhaseReady {
		if desired.ReadyTime == nil {
			now := metav1.Now()
			desired.ReadyTime = &now
		}
		endpoint, nodePort, err := r.resolveEndpoint(ctx, run, svc, pod)
		if err != nil {
			return nil, err
		}
		desired.Endpoint = endpoint
		desired.NodePort = nodePort
	}

	setReadyCondition(desired, run.Generation)
	return desired, nil
}

// currentPod returns the newest pod belonging to the run, or nil if none exist yet.
func (r *TuningRunReconciler) currentPod(ctx context.Context, run *tuningv1alpha1.TuningRun) (*corev1.Pod, error) {
	var pods corev1.PodList
	if err := r.List(ctx, &pods,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{runNameLabel: run.Name},
	); err != nil {
		return nil, err
	}
	if len(pods.Items) == 0 {
		return nil, nil
	}
	sort.Slice(pods.Items, func(i, j int) bool {
		return pods.Items[i].CreationTimestamp.After(pods.Items[j].CreationTimestamp.Time)
	})
	return &pods.Items[0], nil
}

// resolveEndpoint builds the URL a benchmarker uses to reach the engine. For NodePort it is
// http://<node internal IP>:<nodePort>; for ClusterIP it is the in-cluster Service DNS name.
func (r *TuningRunReconciler) resolveEndpoint(ctx context.Context, run *tuningv1alpha1.TuningRun, svc *corev1.Service, pod *corev1.Pod) (string, int32, error) {
	port := run.Spec.Port
	if serviceType(run) == corev1.ServiceTypeNodePort {
		nodePort := serviceNodePort(svc)
		nodeIP, err := r.nodeInternalIP(ctx, pod.Spec.NodeName)
		if err != nil {
			return "", 0, err
		}
		if nodeIP == "" || nodePort == 0 {
			// Not allocated yet; report no endpoint and let the next sync fill it in.
			return "", nodePort, nil
		}
		return fmt.Sprintf("http://%s:%d", nodeIP, nodePort), nodePort, nil
	}
	return fmt.Sprintf("http://%s.%s.svc:%d", svc.Name, svc.Namespace, port), 0, nil
}

// nodeInternalIP returns a node's InternalIP, or "" if the node/address is not found.
func (r *TuningRunReconciler) nodeInternalIP(ctx context.Context, nodeName string) (string, error) {
	if nodeName == "" {
		return "", nil
	}
	var node corev1.Node
	if err := r.Get(ctx, types.NamespacedName{Name: nodeName}, &node); err != nil {
		if apierrors.IsNotFound(err) {
			return "", nil
		}
		return "", err
	}
	for _, addr := range node.Status.Addresses {
		if addr.Type == corev1.NodeInternalIP {
			return addr.Address, nil
		}
	}
	return "", nil
}

// applyStatus writes status only when a meaningful field changed, to avoid a write/reconcile loop.
func (r *TuningRunReconciler) applyStatus(ctx context.Context, run *tuningv1alpha1.TuningRun, desired *tuningv1alpha1.TuningRunStatus) error {
	if statusEqual(&run.Status, desired) {
		return nil
	}
	run.Status = *desired
	return r.Status().Update(ctx, run)
}

// SetupWithManager wires the controller. GenerationChangedPredicate keeps the operator's own status
// writes from re-triggering it; readiness changes still arrive through the owned Deployment.
func (r *TuningRunReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&tuningv1alpha1.TuningRun{}, builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Named("tuningrun").
		Complete(r)
}

// ---------- pure helpers (no client; unit-tested directly) ----------

// buildDeployment renders the single-replica serving Deployment for a run. Defaults are applied here
// too, so the controller is correct even if the apiserver's CRD defaulting did not run (e.g. in tests).
func buildDeployment(run *tuningv1alpha1.TuningRun) *appsv1.Deployment {
	labels := labelsFor(run)
	replicas := int32(1)

	container := corev1.Container{
		Name:            engineContainerName,
		Image:           run.Spec.Image,
		ImagePullPolicy: pullPolicy(run),
		Command:         run.Spec.Command,
		Args:            run.Spec.Args,
		Ports: []corev1.ContainerPort{{
			ContainerPort: run.Spec.Port,
			Protocol:      corev1.ProtocolTCP,
		}},
		Env: envVars(run),
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				HTTPGet: &corev1.HTTPGetAction{
					Path: readinessPath(run),
					Port: intstr.FromInt32(run.Spec.Port),
				},
			},
			InitialDelaySeconds: 10,
			PeriodSeconds:       5,
			TimeoutSeconds:      2,
			FailureThreshold:    3,
		},
	}

	if run.Spec.GPUCount > 0 {
		container.Resources = corev1.ResourceRequirements{
			Limits: corev1.ResourceList{
				corev1.ResourceName(gpuResource(run)): *resource.NewQuantity(int64(run.Spec.GPUCount), resource.DecimalSI),
			},
		}
	}

	var volumes []corev1.Volume
	if vol, mount := modelVolume(run); vol != nil {
		volumes = append(volumes, *vol)
		container.VolumeMounts = append(container.VolumeMounts, *mount)
	}
	if run.Spec.SharedMemoryMB > 0 {
		size := resource.NewQuantity(int64(run.Spec.SharedMemoryMB)*1024*1024, resource.BinarySI)
		volumes = append(volumes, corev1.Volume{
			Name: "dshm",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: size},
			},
		})
		container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{Name: "dshm", MountPath: "/dev/shm"})
	}

	podSpec := corev1.PodSpec{
		Containers:   []corev1.Container{container},
		NodeSelector: run.Spec.NodeSelector,
		Tolerations:  run.Spec.Tolerations,
		Volumes:      volumes,
	}
	if rc := runtimeClass(run); rc != "" {
		podSpec.RuntimeClassName = &rc
	}
	for _, name := range run.Spec.ImagePullSecrets {
		podSpec.ImagePullSecrets = append(podSpec.ImagePullSecrets, corev1.LocalObjectReference{Name: name})
	}

	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: run.Name, Namespace: run.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec:       podSpec,
			},
		},
	}
}

// buildService renders the Service that exposes the run's engine port.
func buildService(run *tuningv1alpha1.TuningRun) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: run.Name, Namespace: run.Namespace, Labels: labelsFor(run)},
		Spec: corev1.ServiceSpec{
			Type:     serviceType(run),
			Selector: labelsFor(run),
			Ports: []corev1.ServicePort{{
				Name:       "http",
				Port:       run.Spec.Port,
				TargetPort: intstr.FromInt32(run.Spec.Port),
				Protocol:   corev1.ProtocolTCP,
			}},
		},
	}
}

// phaseFromPod maps a pod's observed state onto a TuningRun phase, with a machine-readable reason.
func phaseFromPod(pod *corev1.Pod) (tuningv1alpha1.TuningRunPhase, string, string) {
	if reason, msg := containerFailureReason(pod); reason != "" {
		return tuningv1alpha1.PhaseFailed, reason, msg
	}
	if pod.Status.Phase == corev1.PodFailed {
		return tuningv1alpha1.PhaseFailed, "PodFailed", pod.Status.Message
	}
	if isPodReady(pod) {
		return tuningv1alpha1.PhaseReady, "", ""
	}
	if msg := unschedulableMessage(pod); msg != "" {
		return tuningv1alpha1.PhaseStarting, "Unschedulable", msg
	}
	return tuningv1alpha1.PhaseStarting, "", "waiting for the engine to become ready"
}

// containerFailureReason returns a terminal failure reason for the engine container, or "".
func containerFailureReason(pod *corev1.Pod) (string, string) {
	for _, cs := range pod.Status.ContainerStatuses {
		if w := cs.State.Waiting; w != nil && terminalWaitingReasons[w.Reason] {
			return w.Reason, w.Message
		}
		if t := cs.State.Terminated; t != nil {
			if t.Reason == "OOMKilled" {
				return "OOMKilled", t.Message
			}
			if t.ExitCode != 0 {
				return "Error", fmt.Sprintf("container exited with code %d (%s)", t.ExitCode, t.Reason)
			}
		}
		if lt := cs.LastTerminationState.Terminated; lt != nil && lt.Reason == "OOMKilled" {
			return "OOMKilled", lt.Message
		}
	}
	return "", ""
}

func isPodReady(pod *corev1.Pod) bool {
	if pod.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

func unschedulableMessage(pod *corev1.Pod) string {
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodScheduled && c.Status == corev1.ConditionFalse && c.Reason == corev1.PodReasonUnschedulable {
			return c.Message
		}
	}
	return ""
}

func labelsFor(run *tuningv1alpha1.TuningRun) map[string]string {
	return map[string]string{
		runNameLabel:                   run.Name,
		"app.kubernetes.io/managed-by": managerName,
	}
}

func envVars(run *tuningv1alpha1.TuningRun) []corev1.EnvVar {
	if len(run.Spec.Env) == 0 {
		return nil
	}
	keys := make([]string, 0, len(run.Spec.Env))
	for k := range run.Spec.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys) // deterministic order
	env := make([]corev1.EnvVar, 0, len(keys))
	for _, k := range keys {
		env = append(env, corev1.EnvVar{Name: k, Value: run.Spec.Env[k]})
	}
	return env
}

func modelVolume(run *tuningv1alpha1.TuningRun) (*corev1.Volume, *corev1.VolumeMount) {
	mountPath := run.Spec.ModelMountPath
	if mountPath == "" {
		mountPath = defaultModelMountPath
	}
	switch {
	case run.Spec.ModelHostPath != "":
		hpType := corev1.HostPathDirectory
		return &corev1.Volume{
			Name: "model",
			VolumeSource: corev1.VolumeSource{
				HostPath: &corev1.HostPathVolumeSource{Path: run.Spec.ModelHostPath, Type: &hpType},
			},
		}, &corev1.VolumeMount{Name: "model", MountPath: mountPath, ReadOnly: true}
	case run.Spec.ModelPVC != "":
		return &corev1.Volume{
			Name: "model",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: run.Spec.ModelPVC},
			},
		}, &corev1.VolumeMount{Name: "model", MountPath: mountPath, SubPath: run.Spec.ModelSubPath}
	default:
		return nil, nil
	}
}

func runtimeClass(run *tuningv1alpha1.TuningRun) string {
	if run.Spec.RuntimeClassName != nil {
		return *run.Spec.RuntimeClassName
	}
	if run.Spec.GPUCount > 0 {
		return nvidiaRuntimeClass
	}
	return ""
}

func pullPolicy(run *tuningv1alpha1.TuningRun) corev1.PullPolicy {
	if run.Spec.ImagePullPolicy != "" {
		return corev1.PullPolicy(run.Spec.ImagePullPolicy)
	}
	return corev1.PullIfNotPresent
}

func readinessPath(run *tuningv1alpha1.TuningRun) string {
	if run.Spec.ReadinessPath != "" {
		return run.Spec.ReadinessPath
	}
	return defaultReadinessPath
}

func gpuResource(run *tuningv1alpha1.TuningRun) string {
	if run.Spec.GPUResourceName != "" {
		return run.Spec.GPUResourceName
	}
	return defaultGPUResource
}

func serviceType(run *tuningv1alpha1.TuningRun) corev1.ServiceType {
	if run.Spec.ServiceType != "" {
		return corev1.ServiceType(run.Spec.ServiceType)
	}
	return corev1.ServiceType(defaultServiceType)
}

func serviceNodePort(svc *corev1.Service) int32 {
	if svc == nil || len(svc.Spec.Ports) == 0 {
		return 0
	}
	return svc.Spec.Ports[0].NodePort
}

// setReadyCondition mirrors phase into a standard Ready condition.
func setReadyCondition(st *tuningv1alpha1.TuningRunStatus, generation int64) {
	cond := metav1.Condition{Type: readyConditionType, ObservedGeneration: generation}
	switch st.Phase {
	case tuningv1alpha1.PhaseReady:
		cond.Status = metav1.ConditionTrue
		cond.Reason = "EngineReady"
		cond.Message = "engine is serving"
	case tuningv1alpha1.PhaseFailed, tuningv1alpha1.PhaseExpired:
		cond.Status = metav1.ConditionFalse
		cond.Reason = orDefault(st.Reason, string(st.Phase))
		cond.Message = st.Message
	default:
		cond.Status = metav1.ConditionFalse
		cond.Reason = "Progressing"
		cond.Message = st.Message
	}
	apimeta.SetStatusCondition(&st.Conditions, cond)
}

func orDefault(s, fallback string) string {
	if s != "" {
		return s
	}
	return fallback
}

// statusEqual compares the scalar fields that drive the platform. Conditions/times follow from these.
func statusEqual(a, b *tuningv1alpha1.TuningRunStatus) bool {
	return a.Phase == b.Phase &&
		a.Endpoint == b.Endpoint &&
		a.NodePort == b.NodePort &&
		a.PodName == b.PodName &&
		a.NodeName == b.NodeName &&
		a.Reason == b.Reason &&
		a.Message == b.Message &&
		a.ObservedGeneration == b.ObservedGeneration
}
