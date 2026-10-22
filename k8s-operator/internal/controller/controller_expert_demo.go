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

// The request-driven controller-expert demo (deploy/controller-expert-demo/).
//
// One annotation on the PlatformAgent turns it on, and with it off nothing in
// this file changes a render: every hook returns the default it was given.
// With it on, the operator closes the two background paths that do not live in
// the image, and wires the demo tool's two inputs:
//
//   - approvals.cron_mode is "ask" in the machine-global managed scope instead
//     of "approve", so even a schedule someone added by hand could not
//     auto-approve a dangerous command. The demo image ships no schedules at all;
//     this is the second layer, not the first.
//   - the k8s-event-watcher is not started, whatever spec.harness.eventWatcher
//     says. A Warning event posted to the in-pod triage endpoint opens an agent
//     session nobody asked for, which is exactly the kind of unrequested
//     trajectory the demo's gate forbids.
//   - the controller-expert-demo ConfigMap (demo.json, scenarios.jsonl) is
//     mounted read-only into the platform-agent container, and the
//     controller-expert-demo-github Secret's `token` key beside it. Both names
//     are fixed rather than taken from the annotation, so the annotation cannot
//     be used to mount an arbitrary Secret. The kubelet reads the Secret; the
//     agent's ServiceAccount is granted no Secret verb by this or any other
//     path. Neither volume reaches the dashboard, the sidecars or the shell
//     sandbox, where the model's commands would run.

import (
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// controllerExpertDemoAnnotation turns the demo on. Only "true" (trimmed,
	// any case) does; any other value, or none, leaves the render as upstream.
	controllerExpertDemoAnnotation   = "kubeagents.x-k8s.io/controller-expert-demo"
	controllerExpertDemoEnabledValue = "true"
	// controllerExpertDemoCronMode replaces cronModeApprove when the demo is on.
	controllerExpertDemoCronMode = "ask"
	// cronModeApprove is the upstream managed-scope value renderConfigYAML sets.
	cronModeApprove = "approve"

	controllerExpertDemoConfigMapName   = "controller-expert-demo"
	controllerExpertDemoConfigVolume    = "controller-expert-demo-config"
	controllerExpertDemoConfigMountPath = "/etc/controller-expert-demo"

	controllerExpertDemoGitHubSecretName = "controller-expert-demo-github" // #nosec G101 -- Secret name, not a credential.
	controllerExpertDemoGitHubVolume     = "controller-expert-demo-github"
	controllerExpertDemoGitHubMountPath  = "/var/run/secrets/controller-expert-demo/github"
	controllerExpertDemoGitHubKey        = "token"

	// controllerExpertDemoFileMode is read-only for owner and group. The kubelet
	// gives the files the pod's fsGroup (agentFSGroup), the agent's own group, so
	// the agent can read them and no other uid gets anything from "other".
	controllerExpertDemoFileMode int32 = 0o440
	// The EventWatcher condition when the demo, not the spec, keeps the watcher off.
	controllerExpertDemoWatcherReason  = "DisabledByControllerExpertDemo"
	controllerExpertDemoWatcherMessage = "Cluster event ingestion is disabled because this PlatformAgent carries " +
		controllerExpertDemoAnnotation + "=true: the request-driven demo opens no session nobody asked for. " +
		"Remove the annotation to restore upstream behaviour."
	// controllerExpertDemoAgentContainer is the one container that gets the mounts.
	controllerExpertDemoAgentContainer = "platform-agent"
)

// controllerExpertDemoEnabled reports whether the PlatformAgent opted in.
func controllerExpertDemoEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent == nil {
		return false
	}
	value, ok := agent.Annotations[controllerExpertDemoAnnotation]
	return ok && strings.EqualFold(strings.TrimSpace(value), controllerExpertDemoEnabledValue)
}

// controllerExpertDemoCronApprovalMode returns the managed-scope cron_mode.
func controllerExpertDemoCronApprovalMode(agent *agentv1alpha1.PlatformAgent, upstream string) string {
	if controllerExpertDemoEnabled(agent) {
		return controllerExpertDemoCronMode
	}
	return upstream
}

// controllerExpertDemoVolumes are the pod volumes the demo adds, or none.
func controllerExpertDemoVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	if !controllerExpertDemoEnabled(agent) {
		return nil
	}
	return []corev1.Volume{
		{
			Name: controllerExpertDemoConfigVolume,
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: controllerExpertDemoConfigMapName},
					DefaultMode:          ptr.To(controllerExpertDemoFileMode),
				},
			},
		},
		{
			Name: controllerExpertDemoGitHubVolume,
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName:  controllerExpertDemoGitHubSecretName,
					Items:       []corev1.KeyToPath{{Key: controllerExpertDemoGitHubKey, Path: controllerExpertDemoGitHubKey}},
					DefaultMode: ptr.To(controllerExpertDemoFileMode),
				},
			},
		},
	}
}

// mountControllerExpertDemo adds the demo's read-only mounts to the agent
// container and to nothing else.
func mountControllerExpertDemo(agent *agentv1alpha1.PlatformAgent, containers []corev1.Container) {
	if !controllerExpertDemoEnabled(agent) {
		return
	}
	mountIntoContainer(containers, controllerExpertDemoAgentContainer, corev1.VolumeMount{
		Name: controllerExpertDemoConfigVolume, MountPath: controllerExpertDemoConfigMountPath, ReadOnly: true,
	})
	mountIntoContainer(containers, controllerExpertDemoAgentContainer, corev1.VolumeMount{
		Name: controllerExpertDemoGitHubVolume, MountPath: controllerExpertDemoGitHubMountPath, ReadOnly: true,
	})
}

// eventWatcherDisabledCondition names who stopped the watcher, so the status
// does not blame a spec field the CR never set.
func eventWatcherDisabledCondition(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if controllerExpertDemoEnabled(agent) {
		return controllerExpertDemoWatcherReason, controllerExpertDemoWatcherMessage
	}
	return eventWatcherDisabledReason, eventWatcherDisabledMessage
}
