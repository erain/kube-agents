"""Unit tests for the controller-expert demo controller. No network, no model."""

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml

import importlib.util

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("controller_expert_demo", HERE / "controller_expert_demo.py")
demo = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(demo)

# The promoted route, byte-for-byte (k8s-model-training k8s/controller-expert-routing.json).
PROMOTED_ROUTING_SHA256 = "d970f21731bc5bfaa46bed948afe889afcbd10eb27ffb3a0ecdd592f25d13b1e"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assessment", "evidence_ids", "action", "rationale"],
    "properties": {
        "assessment": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "object"},
        "rationale": {"type": "string"},
    },
}
DEPLOY_PATH = "apps/web/deployment.yaml"
SERVICE_PATH = "apps/web/service.yaml"
DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: shop
spec:
  replicas: 4
  strategy:
    rollingUpdate:
      maxSurge: 100%  # owned by the platform team
      maxUnavailable: 0
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: example.com/web:1.2.3
"""
SERVICE = """apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: shop
spec:
  selector:
    app: web
  ports:
    - port: 80
"""
GEN_PATHS = ["apps/new/configmap.yaml", "apps/new/service.yaml"]


def opt_scenario(**overrides):
    row = {
        "id": "opt-surge-01",
        "version": "demo-test-v1",
        "mode": "gitops_read_only",
        "workflow": "optimization",
        "title": "Right-size rollout surge",
        "request": "Reduce the surge budget per the approved contract.",
        "assessment_choices": ["surge_edit", "surge_hold"],
        "allowed_changes": {DEPLOY_PATH: ["spec.strategy.rollingUpdate.maxSurge"], SERVICE_PATH: []},
        "evidence": [{"id": "E1", "text": "contract says 25%"}, {"id": "D1", "text": "unrelated"}],
        "response_schema": SCHEMA,
        "current_files_sha256": {
            DEPLOY_PATH: hashlib.sha256(DEPLOYMENT.encode()).hexdigest(),
            SERVICE_PATH: hashlib.sha256(SERVICE.encode()).hexdigest(),
        },
    }
    row.update(overrides)
    return row


def gen_scenario(**overrides):
    row = {
        "id": "gen-api-01",
        "version": "demo-test-v1",
        "mode": "gitops_read_only",
        "workflow": "generation",
        "title": "Generate config bundle",
        "request": "Create the bundle.",
        "assessment_choices": ["bundle_create"],
        "allowed_changes": {path: ["*"] for path in GEN_PATHS},
        "evidence": [{"id": "E1", "text": "bundle spec"}],
        "response_schema": SCHEMA,
    }
    row.update(overrides)
    return row


def set_fields_response(value="25%", path=DEPLOY_PATH, field="spec.strategy.rollingUpdate.maxSurge", rationale="Approved contract."):
    return {
        "assessment": "surge_edit",
        "evidence_ids": ["E1"],
        "action": {"type": "set_fields", "path": path, "changes": [{"path": field, "value": value}]},
        "rationale": rationale,
    }


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ExpertStub:
    """Records every expert request; answers with a fixed model message."""

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data))
        if self.error:
            raise self.error
        body = {
            "model": self.requests[-1]["model"],
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return FakeResponse(json.dumps(body).encode())


class FakeGitHub:
    def __init__(self, files):
        self.files = dict(files)
        self.opened = []

    def base_commit(self, branch):
        return "a" * 40

    def read_file(self, path, ref):
        return self.files.get(path)

    def open_draft_pr(self, **kwargs):
        self.opened.append(kwargs)
        return {"commit": "b" * 40, "pr_url": "https://github.com/example/gitops/pull/7", "pr_number": "7"}


# Answers the preflight (-output json) with no errors and validates every file.
KUBECONFORM_STUB = """#!/bin/sh
case "$*" in *"-output json"*) echo '{"resources": []}'; exit 0;; esac
echo 'Summary: 1 resource found - Valid: 1'
exit 0
"""


def write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_dir = root / "config"
        self.config_dir.mkdir()
        self.state_dir = root / "state"
        self.agent_home = root / "home"
        (self.agent_home / "cron").mkdir(parents=True)
        (self.agent_home / "cron" / "jobs.json").write_text('{"jobs": []}')
        (self.agent_home / "profiles" / "platform" / "cron").mkdir(parents=True)
        (self.agent_home / "profiles" / "platform" / "cron" / "jobs.json").write_text('{"jobs": []}')
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.kubeconform = bin_dir / "kubeconform"
        write_exe(self.kubeconform, KUBECONFORM_STUB)
        self.kube_linter = bin_dir / "kube-linter"
        write_exe(self.kube_linter, '#!/bin/sh\necho \'{"Reports": []}\'\nexit 0\n')
        self.env = mock.patch.dict(os.environ, {
            demo.CONFIG_DIR_ENV: str(self.config_dir),
            demo.STATE_DIR_ENV: str(self.state_dir),
            demo.LOG_PATH_ENV: str(root / "logs" / "controller-expert-demo.log"),
            demo.AGENT_HOME_ENV: str(self.agent_home),
            demo.KUBECONFORM_ENV: str(self.kubeconform),
            demo.KUBE_LINTER_ENV: str(self.kube_linter),
            demo.ROUTING_PATH_ENV: str(HERE / "routing.json"),
        })
        self.env.start()
        self.write_config()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_config(self, scenarios=None, **overrides):
        scenarios = scenarios if scenarios is not None else [opt_scenario(), gen_scenario()]
        pack = "".join(json.dumps(row, sort_keys=True) + "\n" for row in scenarios)
        (self.config_dir / "scenarios.jsonl").write_text(pack)
        config = {
            "schema_version": 1,
            "expert_base_url": "http://gemma-server.kubeagents-system.svc.cluster.local:8000/v1",
            "github_repository": "example/gitops",
            "base_branch": "main",
            "branch_prefix": "controller-expert/",
            "expert_call_budget": 2,
            "kube_linter_gate": "report",
            "scenario_pack_sha256": hashlib.sha256(pack.encode()).hexdigest(),
        }
        config.update(overrides)
        (self.config_dir / "demo.json").write_text(json.dumps(config))

    def ledger(self):
        return demo.Ledger(self.state_dir).records()


class PromptAndRouteTest(ControllerTestBase):
    def test_prompt_matches_the_evaluated_controller_byte_for_byte(self):
        golden = json.loads((HERE / "testdata" / "prompt_golden.json").read_text())
        for contract, expected in golden["messages"].items():
            self.assertEqual(demo.build_prompt_messages(golden["case"], controller_contract=contract), expected, contract)

    def test_routing_is_the_promoted_route(self):
        self.assertEqual(hashlib.sha256((HERE / "routing.json").read_bytes()).hexdigest(), PROMOTED_ROUTING_SHA256)
        routing = demo.load_routing()
        for workflow in ("troubleshooting", "optimization"):
            route = demo.route_for(routing, workflow, SCHEMA)
            self.assertEqual(route["model"], "loop8-routing-comparison")
            self.assertEqual(route["controller_contract"], "loop8-v1")
            self.assertEqual(route["response_format"]["type"], "json_schema")
            self.assertEqual(route["adapter_sha256"], "66d6b486b21181f9d697d637ba3c3a07cd8b2d640ce416de2037874c1f99db8b")
        route = demo.route_for(routing, "generation", SCHEMA)
        self.assertEqual(route["model"], "loop8-routing-stock")
        self.assertEqual(route["controller_contract"], "v1")
        self.assertEqual(route["response_format"], {"type": "json_object"})
        with self.assertRaises(demo.DemoRefused):
            demo.route_for(routing, "chat", SCHEMA)

    def test_pointer_normalization_and_undeclared_path(self):
        allowed = {DEPLOY_PATH: ["spec.strategy.rollingUpdate.maxSurge"]}
        action = {"type": "set_fields", "path": DEPLOY_PATH, "changes": [{"path": "/spec/strategy/rollingUpdate/maxSurge", "value": "25%"}]}
        self.assertEqual(demo.normalize_action(action, allowed)["changes"][0]["path"], "spec.strategy.rollingUpdate.maxSurge")
        action["changes"][0]["path"] = "spec.replicas"
        with self.assertRaises(demo.ActionContractError):
            demo.normalize_action(action, allowed)


class RunTest(ControllerTestBase):
    def run_demo(self, scenario_id, expert, files=None):
        github = FakeGitHub(files if files is not None else {DEPLOY_PATH: DEPLOYMENT, SERVICE_PATH: SERVICE})
        result = demo.run(scenario_id, expert_urlopen=expert, github=github)
        return result, github

    def test_set_fields_opens_one_draft_pr_with_a_one_line_diff(self):
        expert = ExpertStub(json.dumps(set_fields_response()))
        result, github = self.run_demo("opt-surge-01", expert)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["outcome"], "draft_pr")
        self.assertEqual(len(expert.requests), 1)
        sent = expert.requests[0]
        self.assertEqual(sent["model"], "loop8-routing-comparison")
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["temperature"], 0)
        self.assertEqual(result["checks"]["semantic_diff_paths"], ["spec.strategy.rollingUpdate.maxSurge"])
        self.assertEqual(result["checks"]["serialization"], "surgical")
        self.assertEqual(len(github.opened), 1)
        written = github.opened[0]["files"]
        self.assertEqual(list(written), [DEPLOY_PATH])
        diff = [(a, b) for a, b in zip(DEPLOYMENT.splitlines(), written[DEPLOY_PATH].splitlines()) if a != b]
        self.assertEqual(diff, [("      maxSurge: 100%  # owned by the platform team", "      maxSurge: 25%  # owned by the platform team")])
        self.assertTrue(github.opened[0]["branch"].startswith("controller-expert/opt-surge-01-"))
        self.assertIn("Expert-call budget: 1/2", github.opened[0]["body"])
        self.assertFalse(result["direct_cluster_mutation"])
        events = [record["event"] for record in self.ledger()]
        self.assertEqual(events, ["expert_call_started", "scenario_finished"])
        log_text = Path(os.environ[demo.LOG_PATH_ENV]).read_text()
        self.assertIn("expert_call_budget used=1/2 scenario=opt-surge-01", log_text)

    def test_a_scenario_is_never_retried(self):
        self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response(value="10%", field="spec.replicas"))))
        expert = ExpertStub(json.dumps(set_fields_response()))
        with self.assertRaisesRegex(demo.DemoRefused, "already attempted"):
            self.run_demo("opt-surge-01", expert)
        self.assertEqual(expert.requests, [])

    def test_budget_is_enforced_across_scenarios(self):
        self.write_config(expert_call_budget=1)
        self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response())))
        expert = ExpertStub("{}")
        with self.assertRaisesRegex(demo.DemoRefused, "budget exhausted"):
            self.run_demo("gen-api-01", expert, files={})
        self.assertEqual(expert.requests, [])

    def test_any_cron_job_blocks_the_tool(self):
        (self.agent_home / "profiles" / "platform" / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "compliance-audit"}]}))
        expert = ExpertStub(json.dumps(set_fields_response()))
        with self.assertRaisesRegex(demo.DemoRefused, "background schedules"):
            self.run_demo("opt-surge-01", expert)
        self.assertEqual(expert.requests, [])

    def test_gate_oracle_is_refused_in_cluster(self):
        self.write_config(scenarios=[opt_scenario(expected={"assessment": "surge_edit"})])
        with self.assertRaisesRegex(demo.DemoRefused, "gate-oracle"):
            self.run_demo("opt-surge-01", ExpertStub("{}"))

    def test_unknown_workflow_is_refused_before_inference(self):
        self.write_config(scenarios=[opt_scenario(workflow="chat")])
        expert = ExpertStub("{}")
        with self.assertRaisesRegex(demo.DemoRefused, "unsupported controller workflow"):
            self.run_demo("opt-surge-01", expert)
        self.assertEqual(expert.requests, [])

    def test_pack_digest_mismatch_is_refused(self):
        self.write_config(scenario_pack_sha256="0" * 64)
        with self.assertRaisesRegex(demo.DemoRefused, "scenario_pack_sha256"):
            self.run_demo("opt-surge-01", ExpertStub("{}"))

    def test_base_branch_drift_is_refused_before_inference(self):
        expert = ExpertStub(json.dumps(set_fields_response()))
        with self.assertRaisesRegex(demo.DemoRefused, "differs from the frozen"):
            self.run_demo("opt-surge-01", expert, files={DEPLOY_PATH: DEPLOYMENT.replace("replicas: 4", "replicas: 5"), SERVICE_PATH: SERVICE})
        self.assertEqual(expert.requests, [])

    def test_undeclared_field_fails_without_a_pr_and_spends_the_call(self):
        result, github = self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response(field="spec.replicas", value=9))))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["stage"], "validation")
        self.assertIn("action_normalization", result["error"])
        self.assertEqual(github.opened, [])
        self.assertEqual(result["budget_used"], 1)

    def test_direct_mutation_text_fails(self):
        result, github = self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response(rationale="then kubectl apply -f it"))))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("direct cluster mutation", result["error"])
        self.assertEqual(github.opened, [])

    def test_unparseable_response_fails_without_retry(self):
        expert = ExpertStub("not json at all")
        result, github = self.run_demo("opt-surge-01", expert)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("response_parse", result["error"])
        self.assertEqual(len(expert.requests), 1)

    def test_expert_transport_error_is_final(self):
        expert = ExpertStub(error=urllib.error.URLError("connection refused"))
        result, github = self.run_demo("opt-surge-01", expert)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["stage"], "expert_call")
        self.assertEqual(result["expert_attempts"], 1)
        self.assertEqual(len(expert.requests), 1)
        with self.assertRaisesRegex(demo.DemoRefused, "already attempted"):
            self.run_demo("opt-surge-01", ExpertStub("{}"))

    def test_no_change_passes_without_a_pr(self):
        response = {"assessment": "surge_hold", "evidence_ids": ["E1"], "action": {"type": "no_change"}, "rationale": "Hold."}
        result, github = self.run_demo("opt-surge-01", ExpertStub(json.dumps(response)))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["outcome"], "no_change")
        self.assertEqual(github.opened, [])

    def test_generation_routes_to_stock_and_writes_exact_paths(self):
        objects = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "new", "namespace": "shop"}, "data": {"a": "b"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "new", "namespace": "shop"}, "spec": {"ports": [{"port": 80}]}},
        ]
        response = {
            "assessment": "bundle_create",
            "evidence_ids": ["E1"],
            "action": {"type": "write_files", "files": [{"path": p, "object": o} for p, o in zip(GEN_PATHS, objects)]},
            "rationale": "Bundle.",
        }
        expert = ExpertStub(json.dumps(response))
        result, github = self.run_demo("gen-api-01", expert, files={})
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(expert.requests[0]["model"], "loop8-routing-stock")
        self.assertEqual(expert.requests[0]["response_format"], {"type": "json_object"})
        self.assertEqual(sorted(github.opened[0]["files"]), GEN_PATHS)
        self.assertEqual(yaml.safe_load(github.opened[0]["files"][GEN_PATHS[0]])["kind"], "ConfigMap")

    def test_generation_refuses_to_overwrite_existing_files(self):
        expert = ExpertStub("{}")
        with self.assertRaisesRegex(demo.DemoRefused, "already exist"):
            self.run_demo("gen-api-01", expert, files={GEN_PATHS[0]: "kind: ConfigMap\n"})
        self.assertEqual(expert.requests, [])

    def test_kubeconform_failure_blocks_the_pr(self):
        write_exe(self.kubeconform, KUBECONFORM_STUB.replace("echo 'Summary", "for f; do :; done; grep -q 'maxSurge: 25%' \"$f\" && { echo invalid; exit 1; }\necho 'Summary"))
        result, github = self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response())))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("strict kubeconform failed", result["error"])
        self.assertEqual(github.opened, [])

    def test_schema_fetch_error_refuses_before_inference(self):
        write_exe(self.kubeconform, "#!/bin/sh\necho '{\"resources\": [{\"kind\": \"Deployment\", \"status\": \"statusError\", \"msg\": \"could not find schema\"}]}'\n")
        expert = ExpertStub(json.dumps(set_fields_response()))
        with self.assertRaisesRegex(demo.DemoRefused, "could not load schemas"):
            self.run_demo("opt-surge-01", expert)
        self.assertEqual(expert.requests, [])
        self.assertEqual(self.ledger(), [])

    def test_invalid_base_files_refuse_before_inference(self):
        write_exe(self.kubeconform, KUBECONFORM_STUB.replace("echo 'Summary", "echo invalid; exit 1\necho 'Summary"))
        expert = ExpertStub(json.dumps(set_fields_response()))
        with self.assertRaisesRegex(demo.DemoRefused, "before any model call"):
            self.run_demo("opt-surge-01", expert)
        self.assertEqual(expert.requests, [])

    def test_kube_linter_no_new_findings_gate(self):
        self.write_config(kube_linter_gate="no_new_findings")
        report = {"Reports": [{"Check": "no-read-only-root-fs", "Object": {"K8sObject": {"Name": "web", "GroupVersionKind": {"Kind": "Deployment"}}}}]}
        write_exe(self.kube_linter, "#!/bin/sh\ngrep -q 'maxSurge: 25%' \"$4\" && echo '" + json.dumps(report) + "' || echo '{\"Reports\": []}'\n")
        result, github = self.run_demo("opt-surge-01", ExpertStub(json.dumps(set_fields_response())))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("kube-linter found new findings", result["error"])
        self.assertEqual(github.opened, [])


class GitHubClientTest(unittest.TestCase):
    def test_draft_pr_sequence(self):
        calls = []
        replies = iter([
            {"tree": {"sha": "t0"}},
            {"sha": "t1"},
            {"sha": "c1"},
            {"ref": "refs/heads/x"},
            {"html_url": "https://github.com/o/r/pull/3", "number": 3},
        ])

        def urlopen(request, timeout=None):
            calls.append((request.get_method(), request.full_url, json.loads(request.data) if request.data else None))
            return FakeResponse(json.dumps(next(replies)).encode())

        gh = demo.GitHub("o/r", "token-value", urlopen=urlopen)
        opened = gh.open_draft_pr(base_branch="main", base_sha="s0", branch="controller-expert/x", files={"a.yaml": "k: v\n"}, message="m", title="t", body="b")
        self.assertEqual(opened, {"commit": "c1", "pr_url": "https://github.com/o/r/pull/3", "pr_number": "3"})
        self.assertEqual([c[0] for c in calls], ["GET", "POST", "POST", "POST", "POST"])
        self.assertTrue(calls[-1][2]["draft"])
        self.assertEqual(calls[3][2]["ref"], "refs/heads/controller-expert/x")
        self.assertEqual(calls[2][2]["parents"], ["s0"])


class ConfigValidationTest(ControllerTestBase):
    def test_branch_prefix_cannot_target_the_base(self):
        self.write_config(branch_prefix="main/")
        with self.assertRaisesRegex(demo.DemoRefused, "base branch"):
            demo.load_demo_config()

    def test_budget_must_be_positive(self):
        self.write_config(expert_call_budget=0)
        with self.assertRaises(demo.DemoRefused):
            demo.load_demo_config()

    def test_unsafe_paths_are_refused(self):
        self.write_config(scenarios=[opt_scenario(allowed_changes={"../etc/passwd": []})])
        with self.assertRaisesRegex(demo.DemoRefused, "unsafe repository path"):
            demo.load_scenario(demo.load_demo_config(), "opt-surge-01")


if __name__ == "__main__":
    unittest.main()
