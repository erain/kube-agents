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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func demoAgent(annotation string) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "p", Location: "us-central1-a", ClusterName: "c"},
		},
	}
	if annotation != "" {
		agent.Annotations = map[string]string{controllerExpertDemoAnnotation: annotation}
	}
	return agent
}

func TestControllerExpertDemoEnabledOnlyForTrue(t *testing.T) {
	for value, want := range map[string]bool{"": false, "false": false, "yes": false, "true": true, " TRUE ": true} {
		if got := controllerExpertDemoEnabled(demoAgent(value)); got != want {
			t.Errorf("annotation %q: enabled=%v, want %v", value, got, want)
		}
	}
	if controllerExpertDemoEnabled(nil) {
		t.Error("nil agent reported enabled")
	}
}

func TestControllerExpertDemoPinsCronModeAsk(t *testing.T) {
	upstream := buildConfigMap(demoAgent(""), nil).Data[managedConfigKey]
	if !strings.Contains(upstream, "cron_mode: approve") {
		t.Fatalf("upstream render lost cron_mode: approve:\n%s", upstream)
	}
	demo := buildConfigMap(demoAgent("true"), nil).Data[managedConfigKey]
	if !strings.Contains(demo, "cron_mode: ask") || strings.Contains(demo, "cron_mode: approve") {
		t.Fatalf("demo render must pin cron_mode: ask:\n%s", demo)
	}
}

func TestControllerExpertDemoStopsTheEventWatcher(t *testing.T) {
	if !eventWatcherEnabled(demoAgent("")) {
		t.Fatal("upstream default must keep the watcher on")
	}
	agent := demoAgent("true")
	agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(true)}
	if eventWatcherEnabled(agent) {
		t.Fatal("the demo must stop the watcher even when the CR asks for it")
	}
}

func TestControllerExpertDemoMountsOnlyIntoTheAgentContainer(t *testing.T) {
	spec := buildPodTemplateSpec(demoAgent("true"), "", "", "", "", nil, renderOptions{}).Spec
	volumes := map[string]corev1.Volume{}
	for _, volume := range spec.Volumes {
		volumes[volume.Name] = volume
	}
	secretVolume, ok := volumes[controllerExpertDemoGitHubVolume]
	if !ok || secretVolume.Secret == nil || secretVolume.Secret.SecretName != controllerExpertDemoGitHubSecretName {
		t.Fatalf("missing demo GitHub Secret volume: %+v", secretVolume)
	}
	if len(secretVolume.Secret.Items) != 1 || secretVolume.Secret.Items[0].Key != controllerExpertDemoGitHubKey {
		t.Fatalf("demo Secret volume must project only the token key: %+v", secretVolume.Secret.Items)
	}
	if cm, ok := volumes[controllerExpertDemoConfigVolume]; !ok || cm.ConfigMap == nil || cm.ConfigMap.Name != controllerExpertDemoConfigMapName {
		t.Fatalf("missing demo ConfigMap volume: %+v", cm)
	}
	all := append(append([]corev1.Container{}, spec.InitContainers...), spec.Containers...)
	for _, container := range all {
		for _, mount := range container.VolumeMounts {
			if mount.Name != controllerExpertDemoGitHubVolume && mount.Name != controllerExpertDemoConfigVolume {
				continue
			}
			if container.Name != controllerExpertDemoAgentContainer {
				t.Errorf("demo volume %s mounted into %s", mount.Name, container.Name)
			}
			if !mount.ReadOnly {
				t.Errorf("demo volume %s is not read-only in %s", mount.Name, container.Name)
			}
		}
	}
	upstream := buildPodTemplateSpec(demoAgent(""), "", "", "", "", nil, renderOptions{}).Spec
	for _, volume := range upstream.Volumes {
		if volume.Name == controllerExpertDemoGitHubVolume || volume.Name == controllerExpertDemoConfigVolume {
			t.Errorf("upstream render carries demo volume %s", volume.Name)
		}
	}
}

func TestControllerExpertDemoKeepsAgentRBACReadOnly(t *testing.T) {
	role := buildMinimalPlatformRole(demoAgent("true"))
	for _, rule := range role.Rules {
		for _, resource := range rule.Resources {
			if resource == "secrets" {
				t.Errorf("agent ClusterRole grants %v on secrets", rule.Verbs)
			}
		}
		for _, verb := range rule.Verbs {
			switch verb {
			case "get", "list", "watch":
			default:
				t.Errorf("agent ClusterRole grants mutating verb %q on %v", verb, rule.Resources)
			}
		}
	}
}
