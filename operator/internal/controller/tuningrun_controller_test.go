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
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tuningv1alpha1 "github.com/modelsphere/llm-autotune/operator/api/v1alpha1"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("clientgoscheme: %v", err)
	}
	if err := tuningv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("tuning scheme: %v", err)
	}
	return s
}

func sampleRun() *tuningv1alpha1.TuningRun {
	return &tuningv1alpha1.TuningRun{
		ObjectMeta: metav1.ObjectMeta{Name: "autotune-run-1", Namespace: "autotune"},
		Spec: tuningv1alpha1.TuningRunSpec{
			Image:         "registry.example.com/sglang:test",
			Command:       []string{"python3", "-m", "sglang.launch_server"},
			Args:          []string{"--model-path=/models/m", "--port=8000"},
			Port:          8000,
			GPUCount:      2,
			ModelHostPath: "/mnt/disk0/models",
			ServiceType:   "NodePort",
		},
	}
}

func runKey(run *tuningv1alpha1.TuningRun) types.NamespacedName {
	return types.NamespacedName{Name: run.Name, Namespace: run.Namespace}
}

// --- pure render tests ---

func TestBuildDeployment(t *testing.T) {
	run := sampleRun()
	run.Spec.Env = map[string]string{"B_VAR": "2", "A_VAR": "1"} // out of order on purpose
	run.Spec.SharedMemoryMB = 1024

	dep := buildDeployment(run)

	if dep.Spec.Replicas == nil || *dep.Spec.Replicas != 1 {
		t.Fatalf("want 1 replica, got %v", dep.Spec.Replicas)
	}
	c := dep.Spec.Template.Spec.Containers[0]

	gpu := c.Resources.Limits[corev1.ResourceName("nvidia.com/gpu")]
	if gpu.Value() != 2 {
		t.Errorf("gpu limit = %d, want 2", gpu.Value())
	}
	if rc := dep.Spec.Template.Spec.RuntimeClassName; rc == nil || *rc != "nvidia" {
		t.Errorf("runtimeClassName = %v, want nvidia", rc)
	}
	if c.ReadinessProbe == nil || c.ReadinessProbe.HTTPGet == nil {
		t.Fatal("missing readiness probe")
	}
	if got := c.ReadinessProbe.HTTPGet.Path; got != "/v1/models" {
		t.Errorf("readiness path = %q, want /v1/models", got)
	}
	if got := c.ReadinessProbe.HTTPGet.Port.IntValue(); got != 8000 {
		t.Errorf("readiness port = %d, want 8000", got)
	}
	// env must be deterministic (sorted by key)
	if len(c.Env) != 2 || c.Env[0].Name != "A_VAR" || c.Env[1].Name != "B_VAR" {
		t.Errorf("env not sorted deterministically: %+v", c.Env)
	}
	// model hostPath mount, read-only, default mount path
	var foundModel, foundShm bool
	for _, m := range c.VolumeMounts {
		if m.Name == "model" {
			foundModel = true
			if m.MountPath != "/models" || !m.ReadOnly {
				t.Errorf("model mount = %+v, want /models ro", m)
			}
		}
		if m.Name == "dshm" && m.MountPath == "/dev/shm" {
			foundShm = true
		}
	}
	if !foundModel {
		t.Error("model volume mount missing")
	}
	if !foundShm {
		t.Error("dshm mount missing though SharedMemoryMB set")
	}
}

func TestBuildDeploymentNoGPU(t *testing.T) {
	run := sampleRun()
	run.Spec.GPUCount = 0
	dep := buildDeployment(run)
	c := dep.Spec.Template.Spec.Containers[0]
	if len(c.Resources.Limits) != 0 {
		t.Errorf("expected no resource limits for gpuCount=0, got %v", c.Resources.Limits)
	}
	if dep.Spec.Template.Spec.RuntimeClassName != nil {
		t.Errorf("expected no runtimeClass for gpuCount=0, got %v", *dep.Spec.Template.Spec.RuntimeClassName)
	}
}

// A GPU node is commonly tainted so that pods with no use for cards keep off
// it; the pod that ASKS for cards is what the node is being kept for. The
// platform decides which taints a run may ignore — the operator's only job is
// to not lose them, which is exactly what the CRD did before it had the field.
func TestBuildDeploymentCarriesTolerations(t *testing.T) {
	run := sampleRun()
	run.Spec.Tolerations = []corev1.Toleration{
		{Key: "nvidia.com/gpu", Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule},
		{Key: "dedicated", Operator: corev1.TolerationOpEqual, Value: "ml", Effect: corev1.TaintEffectNoSchedule},
	}

	tol := buildDeployment(run).Spec.Template.Spec.Tolerations

	if len(tol) != 2 {
		t.Fatalf("tolerations = %+v, want 2", tol)
	}
	if tol[0].Key != "nvidia.com/gpu" || tol[0].Operator != corev1.TolerationOpExists {
		t.Errorf("gpu toleration = %+v", tol[0])
	}
	if tol[1].Value != "ml" || tol[1].Effect != corev1.TaintEffectNoSchedule {
		t.Errorf("declared toleration = %+v", tol[1])
	}
}

func TestBuildDeploymentWithoutTolerations(t *testing.T) {
	if tol := buildDeployment(sampleRun()).Spec.Template.Spec.Tolerations; len(tol) != 0 {
		t.Errorf("tolerations = %+v, want none", tol)
	}
}

// One shared weights claim serving many models: the PVC is mounted once and the
// run names its own directory inside it. Without the subPath every run would see
// the whole filesystem at /models and no engine could find its weights.
func TestBuildDeploymentModelPVCSubPath(t *testing.T) {
	run := sampleRun()
	run.Spec.ModelHostPath = ""
	run.Spec.ModelPVC = "model-weights"
	run.Spec.ModelSubPath = "modelforge/release_260817"

	dep := buildDeployment(run)

	var vol *corev1.Volume
	for i := range dep.Spec.Template.Spec.Volumes {
		if dep.Spec.Template.Spec.Volumes[i].Name == "model" {
			vol = &dep.Spec.Template.Spec.Volumes[i]
		}
	}
	if vol == nil || vol.PersistentVolumeClaim == nil {
		t.Fatalf("want a PVC-backed model volume, got %+v", dep.Spec.Template.Spec.Volumes)
	}
	if vol.PersistentVolumeClaim.ClaimName != "model-weights" {
		t.Errorf("claim = %q", vol.PersistentVolumeClaim.ClaimName)
	}
	if vol.HostPath != nil {
		t.Error("hostPath set alongside a PVC")
	}
	for _, m := range dep.Spec.Template.Spec.Containers[0].VolumeMounts {
		if m.Name == "model" {
			if m.SubPath != "modelforge/release_260817" {
				t.Errorf("subPath = %q, want the run's own directory", m.SubPath)
			}
			if m.MountPath != "/models" {
				t.Errorf("mountPath = %q", m.MountPath)
			}
			return
		}
	}
	t.Error("model volume mount missing")
}

func TestBuildServiceNodePort(t *testing.T) {
	svc := buildService(sampleRun())
	if svc.Spec.Type != corev1.ServiceTypeNodePort {
		t.Errorf("service type = %v, want NodePort", svc.Spec.Type)
	}
	if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != 8000 || svc.Spec.Ports[0].TargetPort.IntValue() != 8000 {
		t.Errorf("unexpected ports: %+v", svc.Spec.Ports)
	}
	if svc.Spec.Selector[runNameLabel] != "autotune-run-1" {
		t.Errorf("selector = %v", svc.Spec.Selector)
	}
}

func TestPhaseFromPod(t *testing.T) {
	ready := &corev1.Pod{Status: corev1.PodStatus{
		Phase:      corev1.PodRunning,
		Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
	}}
	imgpull := &corev1.Pod{Status: corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{
		{State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ImagePullBackOff", Message: "no image"}}},
	}}}
	oom := &corev1.Pod{Status: corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{
		{State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{Reason: "OOMKilled", ExitCode: 137}}},
	}}}
	crash := &corev1.Pod{Status: corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{
		{State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "CrashLoopBackOff"}}},
	}}}
	unsched := &corev1.Pod{Status: corev1.PodStatus{
		Phase:      corev1.PodPending,
		Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable, Message: "0/4 nodes"}},
	}}
	starting := &corev1.Pod{Status: corev1.PodStatus{Phase: corev1.PodPending}}

	cases := []struct {
		name       string
		pod        *corev1.Pod
		wantPhase  tuningv1alpha1.TuningRunPhase
		wantReason string
	}{
		{"ready", ready, tuningv1alpha1.PhaseReady, ""},
		{"imagepull", imgpull, tuningv1alpha1.PhaseFailed, "ImagePullBackOff"},
		{"oom", oom, tuningv1alpha1.PhaseFailed, "OOMKilled"},
		{"crashloop", crash, tuningv1alpha1.PhaseFailed, "CrashLoopBackOff"},
		{"unschedulable", unsched, tuningv1alpha1.PhaseStarting, "Unschedulable"},
		{"starting", starting, tuningv1alpha1.PhaseStarting, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			phase, reason, _ := phaseFromPod(tc.pod)
			if phase != tc.wantPhase {
				t.Errorf("phase = %v, want %v", phase, tc.wantPhase)
			}
			if reason != tc.wantReason {
				t.Errorf("reason = %q, want %q", reason, tc.wantReason)
			}
		})
	}
}

// --- reconcile tests (fake client) ---

func TestReconcileCreatesWorkloadAndClusterIPEndpoint(t *testing.T) {
	ctx := context.Background()
	run := sampleRun()
	run.Spec.ServiceType = "ClusterIP"
	s := testScheme(t)
	cl := fake.NewClientBuilder().WithScheme(s).
		WithStatusSubresource(&tuningv1alpha1.TuningRun{}).
		WithObjects(run).Build()
	r := &TuningRunReconciler{Client: cl, Scheme: s}
	req := ctrl.Request{NamespacedName: runKey(run)}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	// Deployment created and owned by the run.
	var dep appsv1.Deployment
	if err := cl.Get(ctx, runKey(run), &dep); err != nil {
		t.Fatalf("deployment not created: %v", err)
	}
	if len(dep.OwnerReferences) == 0 || dep.OwnerReferences[0].Name != run.Name || !*dep.OwnerReferences[0].Controller {
		t.Errorf("deployment not owned by run: %+v", dep.OwnerReferences)
	}
	var svc corev1.Service
	if err := cl.Get(ctx, runKey(run), &svc); err != nil {
		t.Fatalf("service not created: %v", err)
	}

	// No pod yet -> Pending.
	var got tuningv1alpha1.TuningRun
	if err := cl.Get(ctx, runKey(run), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != tuningv1alpha1.PhasePending {
		t.Errorf("phase = %v, want Pending", got.Status.Phase)
	}

	// Inject a ready pod, reconcile again -> Ready + ClusterIP endpoint.
	if err := cl.Create(ctx, readyPod(run, "node1")); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("reconcile 2: %v", err)
	}
	if err := cl.Get(ctx, runKey(run), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != tuningv1alpha1.PhaseReady {
		t.Fatalf("phase = %v, want Ready", got.Status.Phase)
	}
	want := "http://autotune-run-1.autotune.svc:8000"
	if got.Status.Endpoint != want {
		t.Errorf("endpoint = %q, want %q", got.Status.Endpoint, want)
	}
	if got.Status.ReadyTime == nil {
		t.Error("ReadyTime not set")
	}
}

func TestReconcileNodePortEndpoint(t *testing.T) {
	ctx := context.Background()
	run := sampleRun() // NodePort
	s := testScheme(t)
	cl := fake.NewClientBuilder().WithScheme(s).
		WithStatusSubresource(&tuningv1alpha1.TuningRun{}).
		WithObjects(run).Build()
	r := &TuningRunReconciler{Client: cl, Scheme: s}
	req := ctrl.Request{NamespacedName: runKey(run)}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	// Simulate the apiserver allocating a nodePort (the fake client does not).
	var svc corev1.Service
	if err := cl.Get(ctx, runKey(run), &svc); err != nil {
		t.Fatal(err)
	}
	svc.Spec.Ports[0].NodePort = 30055
	if err := cl.Update(ctx, &svc); err != nil {
		t.Fatal(err)
	}
	// A node with an InternalIP, and a ready pod scheduled onto it.
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: "node1"},
		Status:     corev1.NodeStatus{Addresses: []corev1.NodeAddress{{Type: corev1.NodeInternalIP, Address: "198.51.100.5"}}},
	}
	if err := cl.Create(ctx, node); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, readyPod(run, "node1")); err != nil {
		t.Fatal(err)
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	var got tuningv1alpha1.TuningRun
	if err := cl.Get(ctx, runKey(run), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != tuningv1alpha1.PhaseReady {
		t.Fatalf("phase = %v, want Ready", got.Status.Phase)
	}
	if got.Status.NodePort != 30055 {
		t.Errorf("nodePort = %d, want 30055", got.Status.NodePort)
	}
	if want := "http://198.51.100.5:30055"; got.Status.Endpoint != want {
		t.Errorf("endpoint = %q, want %q", got.Status.Endpoint, want)
	}
}

func TestReconcileTTLTearsDown(t *testing.T) {
	ctx := context.Background()
	run := sampleRun()
	run.CreationTimestamp = metav1.NewTime(time.Now().Add(-2 * time.Hour))
	run.Spec.TTLSeconds = 3600
	dep := buildDeployment(run)
	svc := buildService(run)
	s := testScheme(t)
	cl := fake.NewClientBuilder().WithScheme(s).
		WithStatusSubresource(&tuningv1alpha1.TuningRun{}).
		WithObjects(run, dep, svc).Build()
	r := &TuningRunReconciler{Client: cl, Scheme: s}

	if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: runKey(run)}); err != nil {
		t.Fatal(err)
	}

	if err := cl.Get(ctx, runKey(run), &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("deployment should be torn down, got err=%v", err)
	}
	if err := cl.Get(ctx, runKey(run), &corev1.Service{}); !apierrors.IsNotFound(err) {
		t.Errorf("service should be torn down, got err=%v", err)
	}
	var got tuningv1alpha1.TuningRun
	if err := cl.Get(ctx, runKey(run), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != tuningv1alpha1.PhaseExpired {
		t.Errorf("phase = %v, want Expired", got.Status.Phase)
	}
}

func readyPod(run *tuningv1alpha1.TuningRun, node string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      run.Name + "-pod",
			Namespace: run.Namespace,
			Labels:    map[string]string{runNameLabel: run.Name},
		},
		Spec: corev1.PodSpec{NodeName: node},
		Status: corev1.PodStatus{
			Phase:      corev1.PodRunning,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
		},
	}
}
