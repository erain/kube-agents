import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

MODULE_PATH = Path(__file__).with_name("run_demo.py")
SPEC = importlib.util.spec_from_file_location("loop8r_demo", MODULE_PATH)
DEMO = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(DEMO)


class DemoControllerTest(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(DEMO.ASSET.read_text())

    def test_model_prompt_excludes_controller_policy(self):
        messages = DEMO.build_messages(self.case, {"manifest.yaml": "kind: Deployment\n"})
        rendered = json.loads(messages[1]["content"])
        self.assertNotIn("controller_policy", rendered["case"])
        self.assertEqual(rendered["case"]["allowed_action_paths"], [self.case["controller_policy"]["path"]])

    def test_exact_action_applies_one_semantic_change(self):
        policy = self.case["controller_policy"]
        response = {
            "assessment": policy["assessment"],
            "evidence_ids": policy["required_evidence_ids"],
            "action": {"type": "set_fields", "path": policy["path"], "changes": policy["allowed_changes"]},
            "rationale": "Approved evidence establishes the owned rollout budget.",
        }
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            for relative in self.case["repository"]["context_paths"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative == policy["path"]:
                    document = {
                        "apiVersion": "apps/v1", "kind": "Deployment",
                        "metadata": {"name": "demo", "namespace": "demo"},
                        "spec": {"strategy": {"rollingUpdate": {"maxSurge": "100%", "maxUnavailable": 0}}},
                    }
                else:
                    document = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "demo", "namespace": "demo"}}
                path.write_text(yaml.safe_dump(document))
            completed = mock.Mock(returncode=0, stdout="Valid: 1", stderr="")
            with mock.patch.object(DEMO.subprocess, "run", return_value=completed):
                target, checks = DEMO.validate_and_apply(self.case, response, repo)
            self.assertEqual(yaml.safe_load(target.read_text())["spec"]["strategy"]["rollingUpdate"]["maxSurge"], "25%")
            self.assertEqual(checks["semantic_diff_paths"], ["spec.strategy.rollingUpdate.maxSurge"])
            self.assertTrue(all(item["pass"] for item in checks["kubeconform"]))

    def test_direct_mutation_text_is_rejected(self):
        policy = self.case["controller_policy"]
        response = {
            "assessment": policy["assessment"],
            "evidence_ids": policy["required_evidence_ids"],
            "action": {"type": "set_fields", "path": policy["path"], "changes": policy["allowed_changes"]},
            "rationale": "Run kubectl apply after editing.",
        }
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "direct cluster mutation"):
                DEMO.validate_and_apply(self.case, response, Path(temp))


if __name__ == "__main__":
    unittest.main()
