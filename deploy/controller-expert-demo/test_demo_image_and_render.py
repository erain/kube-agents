"""Tests for the demo image overlay script and the rendered-manifest checker."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


profile = load("apply_demo_profile")
check = load("check_rendered")
sys.path.insert(0, str(REPO / "deploy" / "docker"))
import merge_configs  # noqa: E402


def upstream_platform_config():
    base = yaml.safe_load((REPO / "deploy/shared/defaults/config.yaml").read_text())
    overlay = yaml.safe_load((REPO / "agents/platform/config.yaml").read_text())
    return merge_configs.merge(base, overlay)


class DemoProfileTest(unittest.TestCase):
    def test_upstream_config_still_has_the_surface_the_demo_removes(self):
        # If upstream ever drops these, the overlay's assertions are vacuous.
        upstream = upstream_platform_config()
        self.assertIn("platform_control", upstream["mcp_servers"])
        self.assertIn("hermes-cli", upstream["platform_toolsets"]["cli"])
        self.assertIn("kanban", upstream["toolsets"])

    def test_demo_config_keeps_only_the_demo_tool(self):
        config = profile.demo_profile_config(upstream_platform_config())
        profile.check_demo_profile_config(config)
        self.assertEqual(list(config["mcp_servers"]), ["controller_expert_demo"])
        self.assertEqual(config["platform_toolsets"], {"cli": ["mcp-controller_expert_demo"], "api_server": ["mcp-controller_expert_demo"]})
        for denied in ("terminal", "file", "cronjob", "delegation", "web", "skills", "code_execution"):
            self.assertIn(denied, config["agent"]["disabled_toolsets"])
        self.assertEqual(config["toolsets"], [])
        self.assertEqual(config["plugins"]["enabled"], ["hermes_otel", "tool_call_audit"])
        self.assertEqual(config["mcp_servers"]["controller_expert_demo"]["args"], ["/opt/controller-expert-demo/server.py"])

    def test_apply_empties_both_rosters_and_replaces_the_persona(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "opt/defaults/cron").mkdir(parents=True)
            (root / "opt/defaults/cron/jobs.json").write_text((REPO / "agents/chat/defaults/cron/jobs.json").read_text())
            template = root / "opt/platform-template"
            (template / "cron").mkdir(parents=True)
            (template / "cron/jobs.json").write_text((REPO / "agents/platform/cron/jobs.json").read_text())
            (template / "config.yaml").write_text(yaml.safe_dump(upstream_platform_config()))
            for name in ("SOUL.md", "AGENTS.md", "CAPABILITIES.md"):
                (template / name).write_text("upstream\n")
            self.assertTrue(json.loads((template / "cron/jobs.json").read_text())["jobs"])
            profile.apply(root, HERE / "profile")
            self.assertEqual(json.loads((root / "opt/defaults/cron/jobs.json").read_text()), {"jobs": []})
            self.assertEqual(json.loads((template / "cron/jobs.json").read_text()), {"jobs": []})
            profile.check_demo_profile_config(yaml.safe_load((template / "config.yaml").read_text()))
            self.assertIn("run_controller_expert_demo", (template / "SOUL.md").read_text())
            self.assertIn("ONLY", (template / "CAPABILITIES.md").read_text())


def rendered(*docs):
    return "\n---\n".join(yaml.safe_dump(doc) for doc in docs)


def base_docs(**overrides):
    agent = {
        "apiVersion": "kubeagents.x-k8s.io/v1alpha1", "kind": "PlatformAgent",
        "metadata": {"name": "pa", "namespace": "ns", "annotations": {"kubeagents.x-k8s.io/controller-expert-demo": "true"}},
        "spec": {"security": {"serviceAccountName": "agent-sa"}},
    }
    role = {"kind": "ClusterRole", "metadata": {"name": "kubeagents:minimal:ns:pa"}, "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]}
    binding = {"kind": "ClusterRoleBinding", "metadata": {"name": "kubeagents:minimal:ns:pa"}, "roleRef": {"kind": "ClusterRole", "name": "kubeagents:minimal:ns:pa"},
               "subjects": [{"kind": "ServiceAccount", "name": "agent-sa", "namespace": "ns"}]}
    managed = {"kind": "ConfigMap", "metadata": {"name": "pa-config"}, "data": {"managed-config.yaml": "approvals:\n  cron_mode: ask\n"}}
    gateway = {"kind": "Deployment", "metadata": {"name": "pa-gateway"}, "spec": {"template": {"spec": {
        "initContainers": [{"name": "agent-api-auth", "env": [{"name": "EVENT_WATCHER_ENABLED", "value": "false"}]}],
        "containers": [{"name": "platform-agent", "image": "img", "volumeMounts": [
            {"name": "controller-expert-demo-config", "mountPath": "/etc/controller-expert-demo", "readOnly": True},
            {"name": "controller-expert-demo-github", "mountPath": "/var/run/secrets/controller-expert-demo/github", "readOnly": True}]}],
        "volumes": [{"name": "controller-expert-demo-config"}, {"name": "controller-expert-demo-github"}]}}}}
    docs = {"agent": agent, "role": role, "binding": binding, "managed": managed, "gateway": gateway}
    docs.update(overrides)
    return [doc for doc in docs.values() if doc is not None]


class CheckRenderedTest(unittest.TestCase):
    def run_check(self, docs):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rendered.yaml"
            path.write_text(rendered(*docs))
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.check_manifests(path)
            return code, out.getvalue()

    def test_clean_render_passes(self):
        code, out = self.run_check(base_docs())
        self.assertEqual(code, 0, out)

    def test_cronjob_or_schedule_fails(self):
        cronjob = {"kind": "CronJob", "metadata": {"name": "audit"}, "spec": {"schedule": "0 * * * *"}}
        code, out = self.run_check(base_docs(extra=cronjob))
        self.assertEqual(code, 1)
        self.assertIn("CronJob object", out)
        embedded = {"kind": "ConfigMap", "metadata": {"name": "jobs"}, "data": {"jobs.json": json.dumps({"jobs": [{"schedule": {"kind": "cron", "expr": "* * * * *"}}]})}}
        code, out = self.run_check(base_docs(extra=embedded))
        self.assertEqual(code, 1)
        self.assertIn("schedule present", out)

    def test_secret_read_or_write_grant_fails(self):
        role = {"kind": "ClusterRole", "metadata": {"name": "kubeagents:minimal:ns:pa"}, "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]}
        code, out = self.run_check(base_docs(role=role))
        self.assertEqual(code, 1)
        self.assertIn("on secrets", out)
        role = {"kind": "ClusterRole", "metadata": {"name": "kubeagents:minimal:ns:pa"}, "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["create"]}]}
        code, out = self.run_check(base_docs(role=role))
        self.assertEqual(code, 1)
        self.assertIn("['create'] on ['pods']", out)

    def test_cron_mode_watcher_and_mounts_are_enforced(self):
        managed = {"kind": "ConfigMap", "metadata": {"name": "pa-config"}, "data": {"managed-config.yaml": "approvals:\n  cron_mode: approve\n"}}
        code, out = self.run_check(base_docs(managed=managed))
        self.assertEqual(code, 1)
        docs = base_docs()
        docs[-1]["spec"]["template"]["spec"]["initContainers"][0]["env"][0]["value"] = "true"
        docs[-1]["spec"]["template"]["spec"]["initContainers"][0]["volumeMounts"] = [{"name": "controller-expert-demo-github", "readOnly": True}]
        code, out = self.run_check(docs)
        self.assertEqual(code, 1)
        self.assertIn("event watcher not disabled", out)
        self.assertIn("mounted into agent-api-auth", out)

    def test_helm_hook_fails(self):
        hook = {"kind": "Role", "metadata": {"name": "kube-agents-cr-cleanup", "annotations": {"helm.sh/hook": "pre-delete"}}, "rules": []}
        code, out = self.run_check(base_docs(extra=hook))
        self.assertEqual(code, 1)
        self.assertIn("Helm hooks present", out)


if __name__ == "__main__":
    unittest.main()
