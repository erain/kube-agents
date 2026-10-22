#!/opt/hermes/.venv/bin/python3
"""Deterministic controller for the routed GKE controller-expert demo.

The controller owns every mechanic: scenario identity, workflow routing, the
expert-call budget, the one model request, strict action normalization, policy
checks, file application, executable validation, and the GitHub branch, commit
and draft pull request. The model owns one structured decision: assessment,
evidence selection, action and rationale. Nothing here talks to the Kubernetes
API, so no code path can mutate a cluster.

The prompt and the action contract are ports of the evaluated controller in the
GKE controller-expert project (eval/run_leadership_eval.py build_prompt_messages
and eval/controller_action_contract.py). testdata/prompt_golden.json pins the
prompt byte-for-byte against that source so the deployed route is the evaluated
route, not a paraphrase of it.
"""

from __future__ import annotations

import base64
import copy
import datetime
import difflib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

# --- Locations (all overridable for tests through the environment) ---------
# Mounted read-only by the operator from the controller-expert-demo ConfigMap.
CONFIG_DIR_ENV = "CONTROLLER_EXPERT_DEMO_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "/etc/controller-expert-demo"
DEMO_CONFIG_NAME = "demo.json"
SCENARIOS_NAME = "scenarios.jsonl"
# Mounted read-only by the operator from the controller-expert-demo-github Secret.
GITHUB_TOKEN_PATH_ENV = "CONTROLLER_EXPERT_DEMO_GITHUB_TOKEN_PATH"
DEFAULT_GITHUB_TOKEN_PATH = "/var/run/secrets/controller-expert-demo/github/token"
# Baked into the image beside this file; the promoted route, unchanged.
ROUTING_PATH_ENV = "CONTROLLER_EXPERT_DEMO_ROUTING_PATH"
DEFAULT_ROUTING_PATH = str(Path(__file__).resolve().with_name("routing.json"))
# On the agent's persistent volume, so the budget survives a pod restart.
STATE_DIR_ENV = "CONTROLLER_EXPERT_DEMO_STATE_DIR"
DEFAULT_STATE_DIR = "/opt/data/controller-expert-demo"
# Under the gateway's logs/, which the operator's fluent-bit sidecar tails into
# Cloud Logging, so every budget line is visible outside the pod.
LOG_PATH_ENV = "CONTROLLER_EXPERT_DEMO_LOG_PATH"
DEFAULT_LOG_PATH = "/opt/data/logs/controller-expert-demo.log"
# The agent home whose cron rosters must be empty for the tool to run at all.
AGENT_HOME_ENV = "CONTROLLER_EXPERT_DEMO_AGENT_HOME"
DEFAULT_AGENT_HOME = "/opt/data"
KUBECONFORM_ENV = "CONTROLLER_EXPERT_DEMO_KUBECONFORM"
DEFAULT_KUBECONFORM = "/usr/local/bin/kubeconform"
KUBE_LINTER_ENV = "CONTROLLER_EXPERT_DEMO_KUBE_LINTER"
DEFAULT_KUBE_LINTER = "/usr/local/bin/kube-linter"

LEDGER_NAME = "ledger.jsonl"
# kubeconform downloads schemas over HTTPS; the cache on the PVC is filled by a
# preflight BEFORE the expert call, so a schema-fetch failure refuses the run
# instead of failing a scenario whose one expert call was already spent.
KUBECONFORM_CACHE_NAME = "kubeconform-cache"
KUBECONFORM_ERROR_STATUS = "statusError"
# One minimal object per kind a GitOps change here plausibly touches, so their
# schemas are cached before inference. Only fetch errors fail the preflight.
PREFLIGHT_MANIFESTS = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: preflight}
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: preflight}
---
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: preflight}
---
apiVersion: v1
kind: Service
metadata: {name: preflight}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: preflight}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: preflight}
---
apiVersion: v1
kind: Namespace
metadata: {name: preflight}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: preflight}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: preflight}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: preflight}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: preflight}
---
apiVersion: batch/v1
kind: Job
metadata: {name: preflight}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: preflight}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: preflight}
"""
LEDGER_LOCK_NAME = ".ledger.lock"
RESULTS_DIR_NAME = "results"
LOG_PREFIX = "[controller-expert-demo]"

# --- Contract constants -----------------------------------------------------
SCHEMA_VERSION = 1
ALLOWED_WORKFLOWS = frozenset({"troubleshooting", "optimization", "generation"})
OPERATING_MODE = "gitops_read_only"
# Keys that would carry the gate oracle into the cluster. The frozen expected
# answer stays with the out-of-cluster gate; the controller never sees it.
ORACLE_KEYS = frozenset({"expected", "assertions"})
REQUIRED_SCENARIO_KEYS = frozenset({
    "id", "workflow", "title", "request", "mode", "assessment_choices",
    "evidence", "allowed_changes", "response_schema",
})
KUBE_LINTER_GATES = frozenset({"report", "no_new_findings"})
# How the validated change leaves the cluster. "api": the controller opens the
# draft PR itself with the mounted repo-scoped token. "export": no GitHub
# credential is used in the cluster; base files come from a frozen snapshot in
# the ConfigMap, and the validated change is written to the volume as a
# hash-pinned artifact that an operator-side script publishes, unmodified, as a
# draft PR (demo/kube-agents-latest/publish_pr.sh in k8s-model-training).
GITHUB_MODES = frozenset({"api", "export"})
BASE_SNAPSHOT_NAME = "base_snapshot.json"
EXPORTS_DIR_NAME = "exports"
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_MAX_TOKENS = 8192
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
GITHUB_TIMEOUT_SECONDS = 60
VALIDATOR_TIMEOUT_SECONDS = 120
VALIDATOR_DETAIL_CHARS = 1000
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_FILE_MODE = "100644"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
BRANCH_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}/$")
SCENARIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
COMMIT_AUTHOR_NAME = "kube-agents controller-expert demo"
COMMIT_AUTHOR_EMAIL = "controller-expert-demo@kube-agents.invalid"
SYSTEM_PROMPT = (
    "You are the GKE expert decision component inside kube-agents. "
    "Diagnose from supplied evidence and return a safe structured GitOps action. "
    "The controller performs all workflow mechanics and validation."
)
TASK_TEXT = "Return the GKE expert decision for this kube-agents case."
LOOP3_VERSION_PREFIX = "controller-expert-loop3"
DIRECT_MUTATION = re.compile(
    r"\b(kubectl\s+(apply|patch|edit|delete|scale|autoscale|replace|create)|"
    r"gcloud\s+.*\b(delete|update|resize|clusters\s+delete))\b",
    re.I,
)
PATH_PART = re.compile(r"([^\[]+)(?:\[(\d+)\])?$")


class DemoRefused(Exception):
    """Refused before any model call; costs no budget and opens nothing."""


class DemoFailed(Exception):
    """Failed after the model call; recorded as the scenario's final outcome."""


class ActionContractError(ValueError):
    """The model action cannot be normalized without inventing semantics."""


# --- Small helpers -------------------------------------------------------------


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default)


def log(message: str) -> None:
    """One line to stderr (the MCP server's log) and to the fluent-bit-tailed file."""
    line = f"{utc_now()} {LOG_PREFIX} {message}"
    print(line, file=sys.stderr, flush=True)
    log_path = env_path(LOG_PATH_ENV, DEFAULT_LOG_PATH)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # Logging must never be the reason a refusal or a result is lost.
        pass


# --- Configuration -------------------------------------------------------------


def load_routing(path: Path | None = None) -> dict[str, Any]:
    """The promoted route, with the same safety assertions as the evaluated router."""
    payload = json.loads((path or env_path(ROUTING_PATH_ENV, DEFAULT_ROUTING_PATH)).read_text())
    if payload.get("mode") != OPERATING_MODE or payload.get("direct_cluster_mutation") is not False:
        raise DemoRefused("routing configuration must preserve GitOps read-only safety")
    routes = payload.get("routes")
    if not isinstance(routes, dict) or set(routes) != ALLOWED_WORKFLOWS:
        raise DemoRefused("routing configuration must define exactly the supported workflows")
    if payload.get("unknown_workflow_policy") != "reject":
        raise DemoRefused("unknown workflows must be rejected")
    for workflow, route in routes.items():
        if route.get("response_format") not in {"json_schema", "json_object"}:
            raise DemoRefused(f"unsupported response format for {workflow}")
        if route.get("controller_contract") not in {"v1", "loop8-v1"}:
            raise DemoRefused(f"unsupported controller contract for {workflow}")
    return payload


def load_demo_config(config_dir: Path | None = None) -> dict[str, Any]:
    directory = config_dir or env_path(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)
    config = json.loads((directory / DEMO_CONFIG_NAME).read_text())
    if config.get("schema_version") != SCHEMA_VERSION:
        raise DemoRefused("demo.json schema_version must be 1")
    for key in ("expert_base_url", "github_repository", "base_branch", "branch_prefix", "expert_call_budget"):
        if key not in config:
            raise DemoRefused(f"demo.json is missing {key}")
    if not str(config["expert_base_url"]).startswith(("http://", "https://")):
        raise DemoRefused("expert_base_url must be an http(s) URL")
    if not REPOSITORY_PATTERN.fullmatch(str(config["github_repository"])):
        raise DemoRefused("github_repository must be owner/name")
    if not BRANCH_PREFIX_PATTERN.fullmatch(str(config["branch_prefix"])):
        raise DemoRefused("branch_prefix must be a relative ref prefix ending in '/'")
    if str(config["branch_prefix"]).rstrip("/") == str(config["base_branch"]):
        raise DemoRefused("branch_prefix must not name the base branch")
    budget = config["expert_call_budget"]
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise DemoRefused("expert_call_budget must be a positive integer")
    if config.get("kube_linter_gate", "report") not in KUBE_LINTER_GATES:
        raise DemoRefused("kube_linter_gate must be report or no_new_findings")
    mode = config.get("github_mode", "api")
    if mode not in GITHUB_MODES:
        raise DemoRefused("github_mode must be api or export")
    config["github_mode"] = mode
    if mode == "export":
        snapshot_path = directory / BASE_SNAPSHOT_NAME
        if not snapshot_path.is_file():
            raise DemoRefused(f"github_mode export needs {BASE_SNAPSHOT_NAME} in the demo ConfigMap")
        pinned_snapshot = config.get("base_snapshot_sha256")
        if not pinned_snapshot or hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != pinned_snapshot:
            raise DemoRefused(f"{BASE_SNAPSHOT_NAME} does not match demo.json base_snapshot_sha256")
    scenarios_path = directory / SCENARIOS_NAME
    pack_sha = hashlib.sha256(scenarios_path.read_bytes()).hexdigest()
    pinned = config.get("scenario_pack_sha256")
    if pinned and pinned != pack_sha:
        raise DemoRefused("scenarios.jsonl does not match demo.json scenario_pack_sha256")
    config["_scenario_pack_sha256"] = pack_sha
    config["_config_dir"] = str(directory)
    return config


def load_scenario(config: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    if not SCENARIO_ID_PATTERN.fullmatch(scenario_id or ""):
        raise DemoRefused("scenario_id is not a valid identifier")
    found: dict[str, Any] | None = None
    seen: set[str] = set()
    path = Path(config["_config_dir"]) / SCENARIOS_NAME
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise DemoRefused(f"scenarios.jsonl:{line_no} is not an object")
        if ORACLE_KEYS & set(row):
            raise DemoRefused(f"scenarios.jsonl:{line_no} carries gate-oracle keys; keep them out of the cluster")
        row_id = row.get("id")
        if row_id in seen:
            raise DemoRefused(f"duplicate scenario id {row_id!r}")
        seen.add(row_id)
        if row_id == scenario_id:
            found = row
    if found is None:
        raise DemoRefused(f"unknown scenario id {scenario_id!r}")
    missing = REQUIRED_SCENARIO_KEYS - set(found)
    if missing:
        raise DemoRefused(f"scenario is missing {sorted(missing)}")
    if found["workflow"] not in ALLOWED_WORKFLOWS:
        raise DemoRefused(f"unsupported controller workflow: {found['workflow']!r}")
    if found["mode"] != OPERATING_MODE:
        raise DemoRefused("scenario mode must be gitops_read_only")
    allowed = found["allowed_changes"]
    if not isinstance(allowed, dict) or not allowed:
        raise DemoRefused("allowed_changes must be a non-empty object")
    for path_key in list(allowed) + list(found.get("context_paths", [])):
        safe_repo_path(path_key)
    return found


def safe_repo_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts or "\\" in path:
        raise DemoRefused(f"unsafe repository path: {path!r}")
    return path


def route_for(routing: dict[str, Any], workflow: str, response_schema: dict[str, Any]) -> dict[str, Any]:
    """Port of eval/controller_model_router.py route_request."""
    if workflow not in ALLOWED_WORKFLOWS:
        raise DemoRefused(f"unsupported controller workflow: {workflow}")
    route = routing["routes"][workflow]
    if route["response_format"] == "json_schema":
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "gke_expert_action", "strict": True, "schema": response_schema},
        }
    else:
        response_format = {"type": "json_object"}
    return {
        "workflow": workflow,
        "model": route["model"],
        "model_role": route["model_role"],
        "controller_contract": route["controller_contract"],
        "adapter_sha256": route.get("adapter_sha256"),
        "base_snapshot": route.get("base_snapshot"),
        "response_format": response_format,
    }


# --- Background-autonomy guard ----------------------------------------------------


def scheduled_jobs(agent_home: Path | None = None) -> list[str]:
    """Every cron job id present on the agent's volume, across all profiles."""
    home = agent_home or env_path(AGENT_HOME_ENV, DEFAULT_AGENT_HOME)
    stores = [home / "cron" / "jobs.json"] + sorted(home.glob("profiles/*/cron/jobs.json"))
    found: list[str] = []
    for store in stores:
        if not store.is_file():
            continue
        try:
            payload = json.loads(store.read_text() or "{}")
        except (OSError, ValueError):
            found.append(f"{store}:unreadable")
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        for job in jobs or []:
            found.append(f"{store}:{job.get('id', '?') if isinstance(job, dict) else '?'}")
    return found


# --- Prompt (port of eval/run_leadership_eval.py) -------------------------------------


def build_prompt_messages(case: dict[str, Any], *, controller_contract: str) -> list[dict[str, str]]:
    generation_constraint = (
        "For a new workload bundle, use write_files with complete typed Kubernetes JSON objects; do not embed YAML strings."
        if str(case.get("version", "")).startswith(LOOP3_VERSION_PREFIX)
        else "For a new workload bundle, use write_files with complete Kubernetes YAML files."
    )
    constraints = [
        "Return one JSON object only; no markdown.",
        "Use only evidence IDs that directly support the assessment.",
        "Do not emit shell, kubectl, git, gh, or gcloud commands.",
        "The controller owns file editing, validation, branch, commit, push, and PR mechanics.",
        "For an existing manifest, use set_fields and change only required dotted YAML paths.",
        generation_constraint,
        "When evidence does not justify a repository edit, use no_change with no path, changes, or files.",
        "Preserve unrelated fields and never directly mutate the cluster.",
    ]
    if controller_contract == "loop8-v1":
        constraints.extend([
            "Path contract: allowed_action_paths is the exact repository path allowlist, not an example or directory hint.",
            "For write_files, emit exactly one files entry for every allowed_action_path, use each exact path once, and emit no other path.",
            "For set_fields, action.path must exactly equal one allowed_action_path; do not rewrite or shorten it.",
            "Each write_files object must be a complete Kubernetes JSON object whose metadata.name and metadata.namespace match the requested resource identity.",
        ])
    elif controller_contract != "v1":
        raise ValueError(f"unknown controller contract: {controller_contract}")
    payload = {
        "task": TASK_TEXT,
        "operating_mode": case["mode"],
        "constraints": constraints,
        "response_schema": case["response_schema"],
        "case": {
            "id": case["id"],
            "workflow": case["workflow"],
            "title": case["title"],
            "request": case["request"],
            "assessment_choices": case["assessment_choices"],
            "allowed_action_paths": sorted(case["allowed_changes"]),
            "evidence": case["evidence"],
            "current_files": case["current_files"],
        },
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, indent=2, sort_keys=True)},
    ]


def response_from_text(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        if start < 0:
            raise
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
    if not isinstance(obj, dict):
        raise ValueError("response must be a JSON object")
    if isinstance(obj.get("response"), dict) and "assessment" not in obj:
        obj = obj["response"]
    return obj


# --- Action contract (port of eval/controller_action_contract.py) -------------------------


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def pointer_to_dotted(pointer: str) -> str:
    if not pointer.startswith("/"):
        return pointer
    raw_parts = pointer[1:].split("/") if len(pointer) > 1 else []
    parts: list[str] = []
    for raw in raw_parts:
        part = raw.replace("~1", "/").replace("~0", "~")
        if part.isdigit():
            if not parts:
                raise ActionContractError(f"pointer index has no parent: {pointer!r}")
            parts[-1] += f"[{int(part)}]"
        elif part:
            parts.append(part)
        else:
            raise ActionContractError(f"empty pointer segment: {pointer!r}")
    if not parts:
        raise ActionContractError("root JSON Pointer is not an editable field")
    return ".".join(parts)


def _reject_unknown_or_nonempty(action: dict[str, Any], allowed: set[str]) -> None:
    for key, value in action.items():
        if key not in allowed and not _is_empty(value):
            raise ActionContractError(f"non-empty field {key!r} conflicts with action type")


def normalize_action(action: Any, allowed_changes: dict[str, list[str]]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ActionContractError("action must be an object")
    action_type = action.get("type")
    if action_type == "no_change":
        _reject_unknown_or_nonempty(action, {"type"})
        return {"type": "no_change"}
    if action_type == "set_fields":
        _reject_unknown_or_nonempty(action, {"type", "path", "changes"})
        path, changes = action.get("path"), action.get("changes")
        if not isinstance(path, str) or path not in allowed_changes:
            raise ActionContractError(f"set_fields path is not declared: {path!r}")
        if not isinstance(changes, list) or not changes:
            raise ActionContractError("set_fields requires non-empty changes")
        allowed_fields = set(allowed_changes[path])
        normalized: list[dict[str, Any]] = []
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "value"}:
                raise ActionContractError("each change must contain exactly path and value")
            field = change["path"]
            if not isinstance(field, str) or not field:
                raise ActionContractError("change.path must be a non-empty string")
            dotted = pointer_to_dotted(field)
            if dotted not in allowed_fields:
                raise ActionContractError(f"change path is not declared: {field!r}")
            normalized.append({"path": dotted, "value": copy.deepcopy(change["value"])})
        return {"type": "set_fields", "path": path, "changes": normalized}
    if action_type == "write_files":
        _reject_unknown_or_nonempty(action, {"type", "files"})
        files = action.get("files")
        if not isinstance(files, list) or not files:
            raise ActionContractError("write_files requires non-empty files")
        normalized_files: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
                raise ActionContractError("each file requires a non-empty path")
            keys = set(item)
            if keys == {"path", "content"} and isinstance(item["content"], str) and item["content"].strip():
                content = item["content"]
            elif keys == {"path", "object"} and isinstance(item["object"], dict):
                content = yaml.safe_dump(item["object"], sort_keys=False)
            else:
                raise ActionContractError("each file must contain exactly path+content or path+object")
            normalized_files.append({"path": item["path"], "content": content})
        return {"type": "write_files", "files": normalized_files}
    raise ActionContractError(f"unsupported action type: {action_type!r}")


# --- YAML application ------------------------------------------------------------


def _set_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    current = obj
    for offset, token in enumerate(parts):
        match = PATH_PART.fullmatch(token)
        if not match or not isinstance(current, dict) or match.group(1) not in current:
            raise DemoFailed(f"invalid or missing YAML path: {path}")
        key, index = match.group(1), match.group(2)
        last = offset == len(parts) - 1
        if index is None:
            if last:
                current[key] = value
            else:
                current = current[key]
            continue
        sequence = current[key]
        if not isinstance(sequence, list) or int(index) >= len(sequence):
            raise DemoFailed(f"invalid YAML list path: {path}")
        if last:
            sequence[int(index)] = value
        else:
            current = sequence[int(index)]


def _leaves(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            result.update(_leaves(value, f"{prefix}.{key}" if prefix else str(key)))
        return result
    if isinstance(obj, list):
        result = {}
        for index, value in enumerate(obj):
            result.update(_leaves(value, f"{prefix}[{index}]"))
        return result
    return {prefix: obj}


def changed_paths(before: Any, after: Any) -> list[str]:
    b, a = _leaves(before), _leaves(after)
    return sorted(path for path in set(b) | set(a) if b.get(path) != a.get(path))


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, (dict, list)):
        return None
    rendered = yaml.safe_dump(value, default_flow_style=True).strip()
    if rendered.endswith("..."):
        rendered = rendered[: -len("...")].strip()
    return rendered if "\n" not in rendered else None


_YAML_LINE = re.compile(r"^(?P<indent> *)(?P<dash>- +)?(?P<key>[A-Za-z0-9_.\-/]+):(?:[ \t]|$)")


def _key_column(line: str) -> tuple[int, int | None, str | None]:
    """(key column, dash column or None, key or None) of one block-style YAML line."""
    match = _YAML_LINE.match(line)
    if not match:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            return indent + 2, indent, None
        return indent, None, None
    indent = len(match.group("indent"))
    dash = match.group("dash")
    return indent + (len(dash) if dash else 0), (indent if dash else None), match.group("key")


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _path_line(lines: list[str], path: str) -> int | None:
    """Index of the one line holding the scalar at dotted `path`, by walking blocks.

    Used only when the changed key name is not unique in the file (for example a
    livenessProbe port beside a readinessProbe port). Any doubt returns None, and
    surgical_patch's final parse-equality check guards every result.
    """
    start, end, parent = 0, len(lines), -1
    tokens = path.split(".")
    for offset, token in enumerate(tokens):
        match = PATH_PART.fullmatch(token)
        if match is None:
            return None
        key, index = match.group(1), match.group(2)
        block = [i for i in range(start, end) if _significant(lines[i]) and _key_column(lines[i])[0] > parent]
        if not block:
            return None
        child = min(_key_column(lines[i])[0] for i in block)
        hits = [i for i in block if _key_column(lines[i])[0] == child and _key_column(lines[i])[2] == key]
        if len(hits) != 1:
            return None
        found = hits[0]
        column = _key_column(lines[found])[0]
        stop = next((j for j in range(found + 1, end) if _significant(lines[j]) and _key_column(lines[j])[0] <= column), end)
        if offset == len(tokens) - 1:
            return found if index is None else None
        start, end, parent = found + 1, stop, column
        if index is not None:
            items = [i for i in range(start, end) if _significant(lines[i]) and _key_column(lines[i])[1] is not None]
            if not items:
                return None
            dash = min(_key_column(lines[i])[1] for i in items)
            items = [i for i in items if _key_column(lines[i])[1] == dash]
            if int(index) >= len(items):
                return None
            item = items[int(index)]
            following = [i for i in items if i > item]
            start, end, parent = item, (following[0] if following else end), dash
    return None


def surgical_patch(original: str, changes: list[dict[str, Any]], expected: Any) -> str | None:
    """Rewrite only the changed scalar lines when that is provably equivalent.

    A reviewer reads a one-line diff instead of a re-serialized file. The result
    is used only if it parses to exactly the object a full re-serialization would
    produce; otherwise the caller falls back to that full re-serialization.
    """
    text = original
    for change in changes:
        key = change["path"].rsplit(".", 1)[-1]
        if PATH_PART.fullmatch(key) is None or PATH_PART.fullmatch(key).group(2) is not None:
            return None
        rendered = _scalar_text(change["value"])
        if rendered is None:
            return None
        pattern = re.compile(rf"^(\s*{re.escape(key)}:[ \t]*)[^\n#]*?([ \t]*(#.*)?)$", re.MULTILINE)
        if len(pattern.findall(text)) != 1:
            # The key name repeats: locate its line by walking the dotted path.
            lines = text.splitlines(keepends=True)
            index = _path_line(lines, change["path"])
            if index is None:
                return None
            line_pattern = re.compile(rf"^(\s*(?:- +)?{re.escape(key)}:[ \t]*)[^\n#]*?([ \t]*(#.*)?)$")
            body, newline = (lines[index][:-1], "\n") if lines[index].endswith("\n") else (lines[index], "")
            if not line_pattern.fullmatch(body):
                return None
            lines[index] = line_pattern.sub(lambda match, text=rendered: f"{match.group(1)}{text}{match.group(2)}", body) + newline
            text = "".join(lines)
            continue
        text = pattern.sub(lambda match: f"{match.group(1)}{rendered}{match.group(2)}", text)
    try:
        if yaml.safe_load(text) != expected:
            return None
    except yaml.YAMLError:
        return None
    return text


def apply_action(case: dict[str, Any], action: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """Apply a normalized action to the files fetched from the base branch."""
    files = dict(case["current_files"])
    allowed = case["allowed_changes"]
    if action["type"] == "no_change":
        return files, {"changed_files": [], "semantic_diff_paths": []}
    if action["type"] == "set_fields":
        path = action["path"]
        if path not in files:
            raise DemoFailed(f"set_fields target is not a current file: {path}")
        before = yaml.safe_load(files[path])
        after = copy.deepcopy(before)
        for change in action["changes"]:
            _set_path(after, change["path"], copy.deepcopy(change["value"]))
        diff = changed_paths(before, after)
        illegal = sorted(set(diff) - set(allowed.get(path, [])))
        if illegal:
            raise DemoFailed(f"semantic diff leaves the allowlist: {illegal}")
        patched = surgical_patch(files[path], action["changes"], after)
        files[path] = patched if patched is not None else yaml.safe_dump(after, sort_keys=False)
        return files, {
            "changed_files": [path],
            "semantic_diff_paths": diff,
            "serialization": "surgical" if patched is not None else "full",
        }
    # write_files: exactly the declared paths, each once, none pre-existing.
    paths = [item["path"] for item in action["files"]]
    for path in paths:
        safe_repo_path(path)
    if len(paths) != len(set(paths)):
        raise DemoFailed("duplicate write_files path")
    if set(paths) != set(allowed):
        raise DemoFailed(f"write_files paths differ from the allowlist: {sorted(set(paths) ^ set(allowed))}")
    for item in action["files"]:
        files[item["path"]] = item["content"]
    return files, {"changed_files": sorted(paths), "semantic_diff_paths": ["*"]}


# --- Executable validation ----------------------------------------------------------


def _run_tool(argv: list[str], workdir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=workdir, text=True, capture_output=True, check=False, timeout=VALIDATOR_TIMEOUT_SECONDS)


def _kubeconform_argv(kubernetes_version: str | None) -> list[str]:
    cache = env_path(STATE_DIR_ENV, DEFAULT_STATE_DIR) / KUBECONFORM_CACHE_NAME
    cache.mkdir(parents=True, exist_ok=True)
    argv = [str(env_path(KUBECONFORM_ENV, DEFAULT_KUBECONFORM)), "-cache", str(cache)]
    if kubernetes_version:
        argv += ["-kubernetes-version", kubernetes_version]
    return argv


def kubeconform_preflight(files: dict[str, str], kubernetes_version: str | None) -> None:
    """Fill the schema cache and validate the base files, before any model call."""
    with tempfile.TemporaryDirectory(prefix="ced-kubeconform-preflight-") as temp:
        root = Path(temp)
        sample = root / "preflight.yaml"
        sample.write_text(PREFLIGHT_MANIFESTS)
        proc = _run_tool(_kubeconform_argv(kubernetes_version) + ["-output", "json", str(sample)], root)
        try:
            resources = json.loads(proc.stdout or "{}").get("resources") or []
        except ValueError as exc:
            raise DemoRefused(f"kubeconform preflight produced no JSON: {proc.stderr.strip()[-VALIDATOR_DETAIL_CHARS:]}") from exc
        errors = [f"{r.get('kind')}: {r.get('msg')}" for r in resources if r.get("status") == KUBECONFORM_ERROR_STATUS]
        if errors:
            raise DemoRefused(f"kubeconform could not load schemas; refusing before inference: {errors[:3]}")
    base = kubeconform(sorted(files), files, kubernetes_version)
    invalid = [item["path"] for item in base if not item["pass"]]
    if invalid:
        raise DemoRefused(f"base-branch files fail strict kubeconform before any model call: {invalid}")


def kubeconform(paths: list[str], files: dict[str, str], kubernetes_version: str | None) -> list[dict[str, Any]]:
    results = []
    with tempfile.TemporaryDirectory(prefix="ced-kubeconform-") as temp:
        root = Path(temp)
        for path in paths:
            target = root / "f.yaml"
            target.write_text(files[path])
            argv = _kubeconform_argv(kubernetes_version) + ["-strict", "-summary"]
            proc = _run_tool(argv + [str(target)], root)
            detail = (proc.stdout + proc.stderr).strip()[-VALIDATOR_DETAIL_CHARS:]
            results.append({"path": path, "pass": proc.returncode == 0, "detail": detail})
    return results


def kube_linter_findings(paths: list[str], files: dict[str, str]) -> dict[str, list[str]]:
    """Map each path to its sorted list of `check:object` findings."""
    binary = str(env_path(KUBE_LINTER_ENV, DEFAULT_KUBE_LINTER))
    findings: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="ced-kube-linter-") as temp:
        root = Path(temp)
        for path in paths:
            if path not in files:
                findings[path] = []
                continue
            target = root / "f.yaml"
            target.write_text(files[path])
            proc = _run_tool([binary, "lint", "--format", "json", str(target)], root)
            try:
                report = json.loads(proc.stdout or "{}")
            except ValueError as exc:
                raise DemoFailed(f"kube-linter output is not JSON for {path}: {proc.stderr.strip()[-VALIDATOR_DETAIL_CHARS:]}") from exc
            items = []
            for report_item in report.get("Reports") or []:
                obj = ((report_item.get("Object") or {}).get("K8sObject") or {})
                name = f"{obj.get('GroupVersionKind', {}).get('Kind', '?')}/{obj.get('Name', '?')}"
                items.append(f"{report_item.get('Check', '?')}:{name}")
            findings[path] = sorted(items)
    return findings


def validate(case: dict[str, Any], files: dict[str, str], applied: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    changed = applied["changed_files"]
    targets = sorted(set(changed) | {path for path in case["allowed_changes"] if path in files})
    conform = kubeconform(targets, files, config.get("kubernetes_version"))
    if not all(item["pass"] for item in conform):
        failed = [item["path"] for item in conform if not item["pass"]]
        raise DemoFailed(f"strict kubeconform failed: {failed}", {"kubeconform": conform})
    before = kube_linter_findings(changed, case["current_files"])
    after = kube_linter_findings(changed, files)
    new = {path: sorted(set(after[path]) - set(before.get(path, []))) for path in changed}
    new = {path: items for path, items in new.items() if items}
    gate = config.get("kube_linter_gate", "report")
    lint = {"gate": gate, "after": after, "new_findings": new}
    if gate == "no_new_findings" and new:
        raise DemoFailed(f"kube-linter found new findings: {sorted(new)}", {"kubeconform": conform, "kube_linter": lint})
    return {"kubeconform": conform, "kube_linter": lint}


# --- Expert call --------------------------------------------------------------------


def call_expert(
    config: dict[str, Any],
    route: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Exactly one HTTP attempt. Any error is final and is never retried."""
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": 0,
        "max_tokens": int(config.get("max_tokens", DEFAULT_MAX_TOKENS)),
        "response_format": route["response_format"],
    }
    request = urllib.request.Request(
        str(config["expert_base_url"]).rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config.get("expert_bearer", "local"))},
        method="POST",
    )
    started = time.monotonic()
    with urlopen(request, timeout=int(config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS))) as response:
        payload = json.loads(response.read())
    latency = time.monotonic() - started
    raw = payload["choices"][0]["message"].get("content") or ""
    usage = payload.get("usage") or {}
    return {
        "raw": raw,
        "latency_seconds": latency,
        "served_model": payload.get("model", ""),
        "usage": {key: int(usage.get(key) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
    }


# --- Ledger and budget ------------------------------------------------------------------


class Ledger:
    """Append-only JSONL ledger under an exclusive lock; the budget's source of truth."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / LEDGER_NAME
        self.lock_path = state_dir / LEDGER_LOCK_NAME

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def reserve(self, scenario_id: str, run_id: str, budget: int) -> int:
        """Record the start of the one expert call for this scenario, or refuse."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = [r for r in self.records() if r.get("event") == "expert_call_started"]
            if any(r.get("scenario_id") == scenario_id for r in started):
                raise DemoRefused(f"scenario {scenario_id!r} was already attempted; a scenario gets one expert call and is never retried")
            if len(started) >= budget:
                raise DemoRefused(f"expert-call budget exhausted ({len(started)}/{budget})")
            used = len(started) + 1
            self.append({"event": "expert_call_started", "scenario_id": scenario_id, "run_id": run_id, "at": utc_now(), "budget_used": used, "budget": budget})
            return used


# --- GitHub (REST only; there is no git binary in the agent image) -------------------------


class GitHub:
    def __init__(self, repository: str, token: str, *, urlopen: Callable[..., Any] = urllib.request.urlopen):
        self.repository = repository
        self._token = token
        self._urlopen = urlopen

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, allow_missing: bool = False) -> Any:
        request = urllib.request.Request(
            f"{GITHUB_API}/repos/{self.repository}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self._token,
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            raise

    def base_commit(self, branch: str) -> str:
        return self.request("GET", f"/git/ref/heads/{branch}")["object"]["sha"]

    def read_file(self, path: str, ref: str) -> str | None:
        payload = self.request("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", allow_missing=True)
        if payload is None:
            return None
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            raise DemoRefused(f"repository path is not a regular file: {path}")
        return base64.b64decode(payload["content"]).decode()

    def open_draft_pr(self, *, base_branch: str, base_sha: str, branch: str, files: dict[str, str], message: str, title: str, body: str) -> dict[str, str]:
        tree_sha = self.request("GET", f"/git/commits/{base_sha}")["tree"]["sha"]
        tree = self.request("POST", "/git/trees", {
            "base_tree": tree_sha,
            "tree": [{"path": path, "mode": GITHUB_FILE_MODE, "type": "blob", "content": content} for path, content in sorted(files.items())],
        })
        commit = self.request("POST", "/git/commits", {
            "message": message,
            "tree": tree["sha"],
            "parents": [base_sha],
            "author": {"name": COMMIT_AUTHOR_NAME, "email": COMMIT_AUTHOR_EMAIL},
        })
        self.request("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        pull = self.request("POST", "/pulls", {"title": title, "head": branch, "base": base_branch, "body": body, "draft": True})
        return {"commit": commit["sha"], "pr_url": pull["html_url"], "pr_number": str(pull["number"])}


class ExportRepo:
    """The GitHub interface without a credential (github_mode "export").

    Reads the base branch from the frozen, hash-pinned snapshot in the demo
    ConfigMap, and turns "open a draft PR" into one immutable artifact on the
    agent volume. The artifact carries the full content of every changed file,
    their SHA-256s, a unified diff, and the exact commit message, title and
    body; its own SHA-256 is logged and recorded in the result so the operator
    script can prove it publishes exactly what the controller validated.
    """

    def __init__(self, config: dict[str, Any], state_dir: Path):
        snapshot = json.loads((Path(config["_config_dir"]) / BASE_SNAPSHOT_NAME).read_text())
        if snapshot.get("schema_version") != SCHEMA_VERSION:
            raise DemoRefused(f"{BASE_SNAPSHOT_NAME} schema_version must be 1")
        if snapshot.get("repository") != config["github_repository"] or snapshot.get("base_branch") != config["base_branch"]:
            raise DemoRefused(f"{BASE_SNAPSHOT_NAME} names a different repository or base branch than demo.json")
        if not COMMIT_SHA_PATTERN.fullmatch(str(snapshot.get("base_sha", ""))):
            raise DemoRefused(f"{BASE_SNAPSHOT_NAME} base_sha must be a full commit SHA")
        files = snapshot.get("files")
        if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise DemoRefused(f"{BASE_SNAPSHOT_NAME} files must map paths to text")
        for path in files:
            safe_repo_path(path)
        self.repository = str(config["github_repository"])
        self.base_branch = str(config["base_branch"])
        self.base_sha = str(snapshot["base_sha"])
        self.files = dict(files)
        self.exports_dir = state_dir / EXPORTS_DIR_NAME

    def base_commit(self, branch: str) -> str:
        if branch != self.base_branch:
            raise DemoRefused(f"the base snapshot is for {self.base_branch!r}, not {branch!r}")
        return self.base_sha

    def read_file(self, path: str, ref: str) -> str | None:
        if ref != self.base_sha:
            raise DemoRefused("the base snapshot cannot serve a different ref")
        return self.files.get(path)

    def open_draft_pr(self, *, base_branch: str, base_sha: str, branch: str, files: dict[str, str], message: str, title: str, body: str) -> dict[str, str]:
        if base_branch != self.base_branch or base_sha != self.base_sha:
            raise DemoFailed("export does not match the base snapshot")
        diff = "".join(
            "".join(difflib.unified_diff(
                self.files.get(path, "").splitlines(keepends=True), files[path].splitlines(keepends=True),
                fromfile=f"a/{path}" if path in self.files else "/dev/null", tofile=f"b/{path}",
            ))
            for path in sorted(files)
        )
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "kind": "controller-expert-demo-pr-export",
            "repository": self.repository,
            "base_branch": base_branch,
            "base_sha": base_sha,
            "branch": branch,
            "draft": True,
            "commit_message": message,
            "commit_author": {"name": COMMIT_AUTHOR_NAME, "email": COMMIT_AUTHOR_EMAIL},
            "title": title,
            "body": body,
            "files": {path: files[path] for path in sorted(files)},
            "files_sha256": {path: sha256_text(files[path]) for path in sorted(files)},
            "unified_diff": diff,
            "created_at": utc_now(),
        }
        data = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        target = self.exports_dir / (branch.replace("/", "__") + ".json")
        with open(target, "xb") as handle:  # one artifact per branch; never overwritten
            handle.write(data)
        digest = hashlib.sha256(data).hexdigest()
        log(f"pr_export_artifact branch={branch} sha256={digest} file={target}")
        return {"outcome": "draft_pr_export", "export_file": str(target), "export_sha256": digest}


def read_github_token() -> str:
    token = env_path(GITHUB_TOKEN_PATH_ENV, DEFAULT_GITHUB_TOKEN_PATH).read_text().strip()
    if not token:
        raise DemoRefused("the demo GitHub credential file is empty")
    return token


# --- Orchestration ----------------------------------------------------------------------


def pr_body(result: dict[str, Any]) -> str:
    checks = result["checks"]
    lint = checks.get("kube_linter", {})
    lines = [
        "Live kube-agents controller-expert demo (GitOps read-only mode).",
        "",
        f"- Scenario: `{result['scenario_id']}` ({result['workflow']})",
        f"- Expert route: `{result['route']['model']}` ({result['route']['model_role']}, contract `{result['route']['controller_contract']}`)",
        f"- Assessment: `{result['assessment']}`; evidence: {', '.join(result['evidence_ids'])}",
        f"- Semantic diff paths: {', '.join(checks['semantic_diff_paths']) or 'none'}",
        f"- Strict kubeconform: {sum(1 for item in checks['kubeconform'] if item['pass'])}/{len(checks['kubeconform'])} valid",
        f"- kube-linter ({lint.get('gate', 'report')}): {sum(len(v) for v in lint.get('new_findings', {}).values())} new findings",
        f"- Expert-call budget: {result['budget_used']}/{result['budget']}",
        "- Direct cluster mutation: none (the controller has no Kubernetes write path)",
    ]
    if result.get("github_mode") == "export":
        lines.append(
            "- Publication: export mode. No GitHub credential is in the cluster; the controller wrote this "
            "validated change as a hash-pinned artifact and an operator script published it unmodified."
        )
    lines += [
        "",
        "The fine-tuned/stock expert owned the assessment, evidence selection and structured action.",
        "Deterministic controller code owned routing, validation, the commit and this draft PR.",
        "",
        f"Expert response SHA-256: `{result['expert_response_sha256']}`",
        f"Normalized action SHA-256: `{result['normalized_action_sha256']}`",
        f"Scenario pack SHA-256: `{result['scenario_pack_sha256']}`",
    ]
    return "\n".join(lines) + "\n"


def write_result(state_dir: Path, result: dict[str, Any]) -> Path:
    results = state_dir / RESULTS_DIR_NAME
    results.mkdir(parents=True, exist_ok=True)
    target = results / f"{result['run_id']}.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return target


def run(
    scenario_id: str,
    *,
    expert_urlopen: Callable[..., Any] = urllib.request.urlopen,
    github: GitHub | None = None,
) -> dict[str, Any]:
    """Run one frozen scenario end to end. Returns the result record."""
    # Everything up to the reservation is a refusal: no model call, no budget.
    jobs = scheduled_jobs()
    if jobs:
        raise DemoRefused(f"background schedules are present ({len(jobs)}); the demo refuses to run beside autonomy: {jobs[:5]}")
    config = load_demo_config()
    routing = load_routing()
    case = load_scenario(config, scenario_id)
    route = route_for(routing, case["workflow"], case["response_schema"])
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + scenario_id
    branch = f"{config['branch_prefix']}{scenario_id}-{run_id.split('-', 1)[0].lower()}"
    state_dir = env_path(STATE_DIR_ENV, DEFAULT_STATE_DIR)
    ledger = Ledger(state_dir)

    if github is not None:
        gh = github
    elif config["github_mode"] == "export":
        gh = ExportRepo(config, state_dir)
    else:
        gh = GitHub(str(config["github_repository"]), read_github_token())
    base_sha = gh.base_commit(str(config["base_branch"]))
    context_paths = sorted(set(case.get("context_paths", [])) | set(case["allowed_changes"]))
    current_files: dict[str, str] = {}
    for path in context_paths:
        content = gh.read_file(path, base_sha)
        if content is not None:
            current_files[path] = content
    pinned = case.get("current_files_sha256") or {}
    drift = sorted(path for path, digest in pinned.items() if sha256_text(current_files.get(path, "")) != digest)
    if drift:
        raise DemoRefused(f"base branch differs from the frozen scenario files: {drift}")
    if case["workflow"] == "generation":
        existing = sorted(path for path in case["allowed_changes"] if path in current_files)
        if existing:
            raise DemoRefused(f"generation targets already exist on the base branch: {existing}")
    case = dict(case, current_files={path: current_files[path] for path in sorted(current_files) if path in context_paths})
    kubeconform_preflight(case["current_files"], config.get("kubernetes_version"))
    messages = build_prompt_messages(case, controller_contract=route["controller_contract"])

    used = ledger.reserve(scenario_id, run_id, int(config["expert_call_budget"]))
    log(f"expert_call_budget used={used}/{config['expert_call_budget']} scenario={scenario_id} route={route['model']} run={run_id}")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "workflow": case["workflow"],
        "route": {key: route[key] for key in ("model", "model_role", "controller_contract", "adapter_sha256", "base_snapshot")},
        "scenario_pack_sha256": config["_scenario_pack_sha256"],
        "base_sha": base_sha,
        "budget_used": used,
        "budget": config["expert_call_budget"],
        "github_mode": config["github_mode"],
        "expert_attempts": 1,
        "direct_cluster_mutation": False,
    }
    try:
        expert = call_expert(config, route, messages, urlopen=expert_urlopen)
    except Exception as exc:  # recorded as final; never retried
        result.update(status="FAIL", stage="expert_call", error=f"{type(exc).__name__}: {exc}")
        finish(ledger, state_dir, result)
        return result
    result.update(
        expert_latency_seconds=expert["latency_seconds"],
        expert_served_model=expert["served_model"],
        usage=expert["usage"],
        expert_response_sha256=sha256_text(expert["raw"]),
    )
    try:
        try:
            parsed = response_from_text(expert["raw"])
        except ValueError as exc:
            raise DemoFailed(f"response_parse: {exc}") from exc
        if DIRECT_MUTATION.search(json.dumps(parsed, sort_keys=True)):
            raise DemoFailed("direct cluster mutation text rejected")
        try:
            action = normalize_action(parsed.get("action"), case["allowed_changes"])
        except ActionContractError as exc:
            raise DemoFailed(f"action_normalization: {exc}") from exc
        assessment = parsed.get("assessment")
        if assessment not in case["assessment_choices"]:
            raise DemoFailed(f"assessment is not one of the declared choices: {assessment!r}")
        evidence = parsed.get("evidence_ids")
        valid_ids = {item["id"] for item in case["evidence"]}
        if not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) for x in evidence) or not set(evidence) <= valid_ids:
            raise DemoFailed("evidence_ids must be a non-empty subset of the supplied evidence")
        result.update(
            assessment=assessment,
            evidence_ids=list(evidence),
            rationale=str(parsed.get("rationale", "")),
            action_type=action["type"],
            normalized_action_sha256=sha256_text(canonical_json(action)),
        )
        files, applied = apply_action(case, action)
        checks: dict[str, Any] = {"semantic_diff_paths": applied["semantic_diff_paths"], "changed_files": applied["changed_files"]}
        if action["type"] == "no_change":
            checks.update(kubeconform=[], kube_linter={})
            result.update(status="PASS", outcome="no_change", checks=checks)
            finish(ledger, state_dir, result)
            return result
        checks.update(validate(case, files, applied, config))
        if "serialization" in applied:
            checks["serialization"] = applied["serialization"]
        result["checks"] = checks
    except DemoFailed as exc:
        result.update(status="FAIL", stage="validation", error=str(exc.args[0]))
        if len(exc.args) > 1:
            result["checks"] = exc.args[1]
        finish(ledger, state_dir, result)
        return result
    except Exception as exc:  # any other post-call failure is still final
        result.update(status="FAIL", stage="validation", error=f"{type(exc).__name__}: {exc}")
        finish(ledger, state_dir, result)
        return result

    try:
        opened = gh.open_draft_pr(
            base_branch=str(config["base_branch"]),
            base_sha=base_sha,
            branch=branch,
            files={path: files[path] for path in applied["changed_files"]},
            message=f"fix(gitops): {case['title']}"[:72] if action["type"] == "set_fields" else f"feat(gitops): {case['title']}"[:72],
            title=f"[controller-expert demo] {case['title']}",
            body=pr_body(result),
        )
    except Exception as exc:
        result.update(status="FAIL", stage="github", error=f"{type(exc).__name__}: {exc}")
        finish(ledger, state_dir, result)
        return result
    outcome = opened.pop("outcome", "draft_pr")
    result.update(status="PASS", outcome=outcome, branch=branch, **opened)
    finish(ledger, state_dir, result)
    return result


def finish(ledger: Ledger, state_dir: Path, result: dict[str, Any]) -> None:
    result["finished_at"] = utc_now()
    path = write_result(state_dir, result)
    ledger.append({
        "event": "scenario_finished",
        "scenario_id": result["scenario_id"],
        "run_id": result["run_id"],
        "status": result["status"],
        "stage": result.get("stage", "done"),
        "at": result["finished_at"],
        "result_file": str(path),
    })
    log(
        f"scenario_finished scenario={result['scenario_id']} status={result['status']} "
        f"stage={result.get('stage', 'done')} budget={result['budget_used']}/{result['budget']} "
        f"pr={result.get('pr_url', '-')} export_sha256={result.get('export_sha256', '-')}"
    )


def summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status", "outcome", "stage", "error", "scenario_id", "workflow", "route", "assessment",
        "evidence_ids", "action_type", "checks", "pr_url", "commit", "branch", "export_file", "export_sha256",
        "github_mode", "budget_used", "budget",
        "expert_latency_seconds", "usage", "direct_cluster_mutation", "run_id",
    )
    return {key: result[key] for key in keys if key in result}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: controller_expert_demo.py SCENARIO_ID", file=sys.stderr)
        return 2
    try:
        result = run(argv[1])
    except DemoRefused as exc:
        log(f"refused scenario={argv[1]} reason={exc}")
        print(json.dumps({"status": "REFUSED", "error": str(exc), "direct_cluster_mutation": False}))
        return 1
    print(json.dumps(summary(result), sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
