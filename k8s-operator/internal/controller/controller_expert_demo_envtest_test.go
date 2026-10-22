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

// The controller-expert demo's runtime render, captured from the real
// reconciler against a real API server (envtest), not from the builders alone.
//
// The chart renders the PlatformAgent; everything the agent actually runs with —
// its ServiceAccount's Roles and bindings, the managed config, the Deployment,
// the NetworkPolicies — is the operator's, written at reconcile time. This test
// reconciles the demo CR (CONTROLLER_EXPERT_DEMO_CR, the chart-rendered one when
// deploy/controller-expert-demo/render.sh runs it), lists what the operator
// wrote, asserts the demo's invariants on it, and, when
// CONTROLLER_EXPERT_DEMO_RENDER_OUT is set, writes it out as YAML so the same
// objects can be checked independently.

import (
	"context"
	"os"
	"sort"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	nodev1 "k8s.io/api/node/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	controllerExpertDemoCREnv        = "CONTROLLER_EXPERT_DEMO_CR"
	controllerExpertDemoRenderOutEnv = "CONTROLLER_EXPERT_DEMO_RENDER_OUT"
	controllerExpertDemoReconciles   = 3
	controllerExpertDemoRedacted     = "REDACTED-BY-RENDER"
	// controllerExpertDemoGVisorHandler is GKE Sandbox's RuntimeClass handler.
	controllerExpertDemoGVisorHandler = "runsc"
)

func controllerExpertDemoCR(t *testing.T) *agentv1alpha1.PlatformAgent {
	t.Helper()
	path := os.Getenv(controllerExpertDemoCREnv)
	if path == "" {
		agent := demoAgent("true")
		agent.Namespace = "controller-expert-demo"
		return agent
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	agent := &agentv1alpha1.PlatformAgent{}
	if err := yaml.UnmarshalStrict(raw, agent); err != nil {
		t.Fatalf("decoding %s: %v", path, err)
	}
	return agent
}

// renderable strips server-assigned fields so the output reads as manifests.
func renderable(obj client.Object, gvk schema.GroupVersionKind) client.Object {
	obj.GetObjectKind().SetGroupVersionKind(gvk)
	obj.SetManagedFields(nil)
	obj.SetResourceVersion("")
	obj.SetUID("")
	obj.SetCreationTimestamp(metav1.Time{})
	obj.SetGeneration(0)
	obj.SetOwnerReferences(nil)
	return obj
}

func TestControllerExpertDemoRuntimeRenderEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := controllerExpertDemoCR(t)
	if !controllerExpertDemoEnabled(agent) {
		t.Fatalf("the demo CR does not carry %s=true", controllerExpertDemoAnnotation)
	}
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	// The environment GKE provides and the chart does not render: the GKE
	// Sandbox RuntimeClass the CR names. Not part of the captured output.
	if dep := agent.Spec.Deployment; dep != nil && dep.Availability != nil && dep.Availability.RuntimeClassName != nil && *dep.Availability.RuntimeClassName != "" {
		runtimeClass := &nodev1.RuntimeClass{ObjectMeta: metav1.ObjectMeta{Name: *dep.Availability.RuntimeClassName}, Handler: controllerExpertDemoGVisorHandler}
		if err := cl.Create(ctx, runtimeClass); err != nil {
			t.Fatalf("creating RuntimeClass: %v", err)
		}
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	for pass := 0; pass < controllerExpertDemoReconciles; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile pass %d: %v", pass, err)
		}
	}

	reconciled := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, reconciled); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	for _, condition := range reconciled.Status.Conditions {
		t.Logf("condition %s=%s reason=%s: %s", condition.Type, condition.Status, condition.Reason, condition.Message)
	}

	var objects []client.Object
	inNS := client.InNamespace(agent.Namespace)
	collect := func(list client.ObjectList, gvk schema.GroupVersionKind, opts ...client.ListOption) []client.Object {
		t.Helper()
		if err := cl.List(ctx, list, opts...); err != nil {
			t.Fatalf("listing %s: %v", gvk.Kind, err)
		}
		var out []client.Object
		switch typed := list.(type) {
		case *corev1.ConfigMapList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *corev1.SecretList:
			for i := range typed.Items {
				secret := &typed.Items[i]
				for key := range secret.Data {
					secret.Data[key] = []byte(controllerExpertDemoRedacted)
				}
				out = append(out, renderable(secret, gvk))
			}
		case *corev1.ServiceList:
			for i := range typed.Items {
				typed.Items[i].Spec.ClusterIP, typed.Items[i].Spec.ClusterIPs = "", nil
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *corev1.ServiceAccountList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *corev1.PersistentVolumeClaimList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *appsv1.DeploymentList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *appsv1.StatefulSetList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *networkingv1.NetworkPolicyList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *policyv1.PodDisruptionBudgetList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *rbacv1.RoleList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *rbacv1.RoleBindingList:
			for i := range typed.Items {
				out = append(out, renderable(&typed.Items[i], gvk))
			}
		case *rbacv1.ClusterRoleList:
			for i := range typed.Items {
				if strings.HasPrefix(typed.Items[i].Name, "kubeagents:") {
					out = append(out, renderable(&typed.Items[i], gvk))
				}
			}
		case *rbacv1.ClusterRoleBindingList:
			for i := range typed.Items {
				if strings.HasPrefix(typed.Items[i].Name, "kubeagents:") {
					out = append(out, renderable(&typed.Items[i], gvk))
				}
			}
		}
		return out
	}
	v1 := func(kind string) schema.GroupVersionKind { return corev1.SchemeGroupVersion.WithKind(kind) }
	objects = append(objects, collect(&corev1.ServiceAccountList{}, v1("ServiceAccount"), inNS)...)
	objects = append(objects, collect(&corev1.ConfigMapList{}, v1("ConfigMap"), inNS)...)
	objects = append(objects, collect(&corev1.SecretList{}, v1("Secret"), inNS)...)
	objects = append(objects, collect(&corev1.ServiceList{}, v1("Service"), inNS)...)
	objects = append(objects, collect(&corev1.PersistentVolumeClaimList{}, v1("PersistentVolumeClaim"), inNS)...)
	objects = append(objects, collect(&appsv1.DeploymentList{}, appsv1.SchemeGroupVersion.WithKind("Deployment"), inNS)...)
	objects = append(objects, collect(&appsv1.StatefulSetList{}, appsv1.SchemeGroupVersion.WithKind("StatefulSet"), inNS)...)
	objects = append(objects, collect(&networkingv1.NetworkPolicyList{}, networkingv1.SchemeGroupVersion.WithKind("NetworkPolicy"), inNS)...)
	objects = append(objects, collect(&policyv1.PodDisruptionBudgetList{}, policyv1.SchemeGroupVersion.WithKind("PodDisruptionBudget"), inNS)...)
	objects = append(objects, collect(&rbacv1.RoleList{}, rbacv1.SchemeGroupVersion.WithKind("Role"), inNS)...)
	objects = append(objects, collect(&rbacv1.RoleBindingList{}, rbacv1.SchemeGroupVersion.WithKind("RoleBinding"), inNS)...)
	objects = append(objects, collect(&rbacv1.ClusterRoleList{}, rbacv1.SchemeGroupVersion.WithKind("ClusterRole"))...)
	objects = append(objects, collect(&rbacv1.ClusterRoleBindingList{}, rbacv1.SchemeGroupVersion.WithKind("ClusterRoleBinding"))...)

	checkControllerExpertDemoRuntime(t, agent, objects)

	if out := os.Getenv(controllerExpertDemoRenderOutEnv); out != "" {
		sort.SliceStable(objects, func(i, j int) bool {
			ki, kj := objects[i].GetObjectKind().GroupVersionKind().Kind, objects[j].GetObjectKind().GroupVersionKind().Kind
			if ki != kj {
				return ki < kj
			}
			return objects[i].GetName() < objects[j].GetName()
		})
		var b strings.Builder
		b.WriteString("# Operator runtime render for the controller-expert demo CR, captured from the\n")
		b.WriteString("# real reconciler against envtest. Secret values are redacted.\n")
		for _, obj := range objects {
			raw, err := yaml.Marshal(obj)
			if err != nil {
				t.Fatalf("marshal %s/%s: %v", obj.GetObjectKind().GroupVersionKind().Kind, obj.GetName(), err)
			}
			b.WriteString("---\n")
			b.Write(raw)
		}
		if err := os.WriteFile(out, []byte(b.String()), 0o644); err != nil {
			t.Fatalf("writing %s: %v", out, err)
		}
	}
}

// checkControllerExpertDemoRuntime asserts the demo's invariants on what the
// operator actually wrote.
func checkControllerExpertDemoRuntime(t *testing.T, agent *agentv1alpha1.PlatformAgent, objects []client.Object) {
	t.Helper()
	saName := agentServiceAccountName(agent)
	roles := map[string]*rbacv1.Role{}
	clusterRoles := map[string]*rbacv1.ClusterRole{}
	var bindings []struct {
		subjects []rbacv1.Subject
		ref      rbacv1.RoleRef
	}
	var deployment *appsv1.Deployment
	var managed string
	for _, obj := range objects {
		switch typed := obj.(type) {
		case *rbacv1.Role:
			roles[typed.Name] = typed
		case *rbacv1.ClusterRole:
			clusterRoles[typed.Name] = typed
		case *rbacv1.RoleBinding:
			bindings = append(bindings, struct {
				subjects []rbacv1.Subject
				ref      rbacv1.RoleRef
			}{typed.Subjects, typed.RoleRef})
		case *rbacv1.ClusterRoleBinding:
			bindings = append(bindings, struct {
				subjects []rbacv1.Subject
				ref      rbacv1.RoleRef
			}{typed.Subjects, typed.RoleRef})
		case *appsv1.Deployment:
			if typed.Name == agent.Name+"-gateway" {
				deployment = typed
			}
		case *corev1.ConfigMap:
			if value, ok := typed.Data[managedConfigKey]; ok && typed.Name == agent.Name+"-config" {
				managed = value
			}
		}
	}

	// Every rule reachable from the agent's ServiceAccount: reads only, no
	// Secret verb at all. Two non-read grants exist upstream and are named
	// rather than hidden: lease writes for leader election, and TokenReview
	// create, which the credential broker needs and which lands on this
	// ServiceAccount because the broker shares it (buildCredentialBrokerTokenReviewRole).
	for _, binding := range bindings {
		bound := false
		for _, subject := range binding.subjects {
			if subject.Kind == "ServiceAccount" && subject.Name == saName && (subject.Namespace == "" || subject.Namespace == agent.Namespace) {
				bound = true
			}
		}
		if !bound {
			continue
		}
		var rules []rbacv1.PolicyRule
		switch binding.ref.Kind {
		case "Role":
			if role := roles[binding.ref.Name]; role != nil {
				rules = role.Rules
			}
		case "ClusterRole":
			if role := clusterRoles[binding.ref.Name]; role != nil {
				rules = role.Rules
			}
		}
		for _, rule := range rules {
			for _, resource := range rule.Resources {
				if resource == "secrets" || resource == "*" {
					t.Errorf("%s %s grants %v on %s to the agent ServiceAccount", binding.ref.Kind, binding.ref.Name, rule.Verbs, resource)
				}
			}
			mutating := false
			for _, verb := range rule.Verbs {
				switch verb {
				case "get", "list", "watch":
				default:
					mutating = true
				}
			}
			if !mutating {
				continue
			}
			allowed := len(rule.Resources) > 0
			for _, resource := range rule.Resources {
				if resource != "leases" && resource != "tokenreviews" {
					allowed = false
				}
			}
			if !allowed {
				t.Errorf("%s %s grants mutating %v on %v to the agent ServiceAccount", binding.ref.Kind, binding.ref.Name, rule.Verbs, rule.Resources)
			}
		}
	}

	if !strings.Contains(managed, "cron_mode: ask") || strings.Contains(managed, "cron_mode: approve") {
		t.Errorf("managed config must pin cron_mode: ask:\n%s", managed)
	}
	if deployment == nil {
		t.Fatalf("no %s-gateway Deployment was rendered", agent.Name)
	}
	pod := deployment.Spec.Template.Spec
	for _, container := range append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...) {
		for _, env := range container.Env {
			if env.Name == "EVENT_WATCHER_ENABLED" && env.Value != "false" {
				t.Errorf("%s starts the event watcher (EVENT_WATCHER_ENABLED=%s)", container.Name, env.Value)
			}
		}
	}
	found := map[string]bool{}
	for _, volume := range pod.Volumes {
		found[volume.Name] = true
	}
	if !found[controllerExpertDemoConfigVolume] || !found[controllerExpertDemoGitHubVolume] {
		t.Errorf("gateway pod lacks the demo volumes: %v", found)
	}
}
