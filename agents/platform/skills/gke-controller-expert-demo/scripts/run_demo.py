#!/opt/hermes/.venv/bin/python3
"""Run the fixed LOOP 8r leadership demo through live expert inference and GitOps."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ASSET = Path(__file__).resolve().parents[1] / "assets" / "rollout-surge-budget.json"
MUTATION = re.compile(r"\b(kubectl\s+(?:apply|patch|edit|delete|replace|scale)|gcloud\s+container\s+clusters\s+(?:delete|update))\b", re.I)


def log(message: str) -> None:
    print(f"[LOOP8R-DEMO] {message}", file=sys.stderr, flush=True)


def sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def parse_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("expert response is not an object")
    return value


def build_messages(case: dict[str, Any], current_files: dict[str, str]) -> list[dict[str, str]]:
    payload = {
        "task": "Return the GKE expert decision for this kube-agents case.",
        "operating_mode": "gitops_read_only",
        "constraints": [
            "Return one JSON object only; no markdown.",
            "Use only evidence IDs that directly support the assessment.",
            "Do not emit shell, kubectl, git, gh, or gcloud commands.",
            "The controller owns file editing, validation, branch, commit, push, and PR mechanics.",
            "For an existing manifest, use set_fields and change only required dotted YAML paths.",
            "When evidence does not justify a repository edit, use no_change with no path, changes, or files.",
            "Preserve unrelated fields and never directly mutate the cluster.",
            "Path contract: allowed_action_paths is the exact repository path allowlist, not an example or directory hint.",
            "For set_fields, action.path must exactly equal one allowed_action_path; do not rewrite or shorten it.",
        ],
        "response_schema": case["response_schema"],
        "case": {
            "id": case["case_id"],
            "workflow": case["workflow"],
            "title": case["title"],
            "request": case["request"],
            "assessment_choices": case["assessment_choices"],
            "allowed_action_paths": [case["controller_policy"]["path"]],
            "evidence": case["evidence"],
            "current_files": current_files,
        },
    }
    return [
        {"role": "system", "content": "You are the GKE expert decision component inside kube-agents. Diagnose from supplied evidence and return one safe structured GitOps action. The controller performs all workflow mechanics and validation."},
        {"role": "user", "content": json.dumps(payload, indent=2, sort_keys=True)},
    ]


def call_expert(case: dict[str, Any], current_files: dict[str, str]) -> tuple[dict[str, Any], str, dict[str, Any], float]:
    base_url = require_env("EXPERT_BASE_URL").rstrip("/")
    model = require_env("EXPERT_MODEL")
    body = {
        "model": model,
        "messages": build_messages(case, current_files),
        "temperature": 0,
        "max_tokens": 8192,
        "response_format": {"type": "json_schema", "json_schema": {"name": "gke_expert_action", "strict": True, "schema": case["response_schema"]}},
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.loads(response.read())
    latency = time.monotonic() - started
    raw = result["choices"][0]["message"].get("content") or ""
    return parse_response(raw), raw, result.get("usage") or {}, latency


def get_path(document: Any, dotted: str) -> Any:
    current = document
    for token in dotted.split("."):
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", token)
        if not match or not isinstance(current, dict):
            raise ValueError(f"invalid path: {dotted}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            current = current[int(match.group(2))]
    return current


def set_path(document: Any, dotted: str, value: Any) -> None:
    tokens = dotted.split(".")
    parent = document
    for token in tokens[:-1]:
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", token)
        if not match or not isinstance(parent, dict):
            raise ValueError(f"invalid path: {dotted}")
        parent = parent[match.group(1)]
        if match.group(2) is not None:
            parent = parent[int(match.group(2))]
    last = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", tokens[-1])
    if not last or not isinstance(parent, dict):
        raise ValueError(f"invalid path: {dotted}")
    if last.group(2) is None:
        parent[last.group(1)] = value
    else:
        parent[last.group(1)][int(last.group(2))] = value


def leaves(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            output.update(leaves(child, child_prefix))
        return output
    if isinstance(value, list):
        output = {}
        for index, child in enumerate(value):
            output.update(leaves(child, f"{prefix}[{index}]"))
        return output
    return {prefix: value}


def validate_and_apply(case: dict[str, Any], response: dict[str, Any], repo: Path) -> tuple[Path, dict[str, Any]]:
    policy = case["controller_policy"]
    if response.get("assessment") != policy["assessment"]:
        raise ValueError("assessment failed controller policy")
    evidence = response.get("evidence_ids")
    if not isinstance(evidence, list) or not set(policy["required_evidence_ids"]).issubset(evidence):
        raise ValueError("required evidence IDs are missing")
    valid_evidence = {item["id"] for item in case["evidence"]}
    if not set(evidence).issubset(valid_evidence):
        raise ValueError("invalid evidence ID")
    if MUTATION.search(json.dumps(response)):
        raise ValueError("direct cluster mutation text rejected")
    action = response.get("action")
    if not isinstance(action, dict) or action.get("type") != policy["action_type"]:
        raise ValueError("unexpected action type")
    if action.get("path") != policy["path"]:
        raise ValueError("unexpected repository path")
    changes = action.get("changes")
    if changes != policy["allowed_changes"]:
        raise ValueError("action differs from approved path/value contract")

    target = repo / policy["path"]
    before = yaml.safe_load(target.read_text())
    after = json.loads(json.dumps(before))
    for change in changes:
        set_path(after, change["path"], change["value"])
    before_leaves, after_leaves = leaves(before), leaves(after)
    changed = sorted(path for path in set(before_leaves) | set(after_leaves) if before_leaves.get(path) != after_leaves.get(path))
    allowed = sorted(change["path"] for change in changes)
    if changed != allowed:
        raise ValueError(f"semantic diff is not minimal: {changed}")
    target.write_text(yaml.safe_dump(after, sort_keys=False))

    validation = []
    for relative in case["repository"]["context_paths"]:
        path = repo / relative
        proc = subprocess.run(["kubeconform", "-strict", "-summary", str(path)], text=True, capture_output=True, check=False)
        validation.append({"path": relative, "pass": proc.returncode == 0, "detail": (proc.stdout + proc.stderr).strip()[-1000:]})
    if not all(item["pass"] for item in validation):
        raise ValueError("strict kubeconform failed")
    return target, {"semantic_diff_paths": changed, "kubeconform": validation}


def git_env(askpass: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"})
    return env


def run() -> dict[str, Any]:
    case = json.loads(ASSET.read_text())
    token = require_env("GH_TOKEN")
    repo_url = os.environ.get("DEMO_GIT_REPO", case["repository"]["url"])
    base_branch = os.environ.get("DEMO_BASE_BRANCH", case["repository"]["base_branch"])
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    branch = f"platform-agent/loop8r-rollout-budget-{run_id.lower()}"

    with tempfile.TemporaryDirectory(prefix="loop8r-demo-") as temp:
        temp_path = Path(temp)
        askpass = temp_path / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) echo x-access-token;; *) printf "%s" "$GH_TOKEN";; esac\n')
        askpass.chmod(0o700)
        env = git_env(askpass)
        repo = temp_path / "repo"
        subprocess.run(["git", "clone", "--quiet", "--single-branch", "--branch", base_branch, repo_url, str(repo)], check=True, env=env)
        current_files = {relative: (repo / relative).read_text() for relative in case["repository"]["context_paths"]}
        response, raw, usage, latency = call_expert(case, current_files)
        target, checks = validate_and_apply(case, response, repo)

        subprocess.run(["git", "-C", str(repo), "checkout", "-b", branch], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "GKE Platform Agent"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "platform-agent@kube-agents.invalid"], check=True)
        relative_target = str(target.relative_to(repo))
        subprocess.run(["git", "-C", str(repo), "add", "--", relative_target], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "fix(gitops): set approved rollout surge budget"], check=True, capture_output=True, text=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(repo), "push", "origin", f"HEAD:refs/heads/{branch}"], check=True, env=env, capture_output=True, text=True)
        pr_body = (
            "Live kube-agents LOOP 8r leadership demo.\n\n"
            "- Fine-tuned specialist owned assessment and structured action.\n"
            "- Controller enforced exact path/value, minimal semantic diff, and strict kubeconform.\n"
            "- No direct cluster mutation occurred.\n\n"
            f"Expert response SHA-256: `{sha_bytes(raw.encode())}`"
        )
        gh_env = dict(env); gh_env["GH_TOKEN"] = token
        pr_url = subprocess.check_output([
            "gh", "pr", "create", "--draft", "--repo", "erain/k8s-model-training",
            "--base", base_branch, "--head", branch,
            "--title", f"Demo: approved rollout surge budget ({run_id})", "--body", pr_body,
        ], text=True, env=gh_env).strip()

        result = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": run_id,
            "case_id": case["case_id"],
            "expert_model": require_env("EXPERT_MODEL"),
            "expert_base_snapshot": os.environ.get("EXPERT_BASE_SNAPSHOT", ""),
            "expert_adapter_sha256": os.environ.get("EXPERT_ADAPTER_SHA256", ""),
            "expert_response_sha256": sha_bytes(raw.encode()),
            "normalized_action_sha256": sha_bytes(json.dumps(response["action"], sort_keys=True, separators=(",", ":")).encode()),
            "expert_latency_seconds": latency,
            "usage": usage,
            "checks": checks,
            "branch": branch,
            "commit": commit,
            "pr_url": pr_url,
            "direct_cluster_mutation": False,
        }
        results_dir = Path(os.environ.get("DEMO_RESULTS_DIR", "/opt/data/demo-results"))
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / f"{run_id}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result


if __name__ == "__main__":
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception as exc:
        log(f"FAIL: {type(exc).__name__}: {exc}")
        raise
