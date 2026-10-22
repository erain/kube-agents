#!/usr/bin/env python3
"""Prove the controller-expert demo's no-autonomy invariants on rendered output.

Subcommands:

  extract-cr RENDERED OUT        write the chart-rendered PlatformAgent to OUT
  manifests RENDERED             check chart + operator runtime render
  profile BASE OVERLAY [OUT]     build the demo profile config from the upstream
                                 sources exactly as the image does, check it,
                                 and optionally write it

`manifests` fails unless every check holds:

  * no CronJob object, and no non-empty `schedule:` or `cron:` key anywhere,
    including inside YAML carried in ConfigMap data;
  * every Role/ClusterRole bound to the agent's ServiceAccount is read-only
    except lease writes (leader election) and TokenReview create (the
    credential broker's caller check; the broker shares the ServiceAccount),
    and none of them grants any verb on Secrets;
  * the PlatformAgent opts into the demo, the managed config pins
    `approvals.cron_mode: ask`, the event watcher is off, and the demo's two
    volumes are mounted read-only into the platform-agent container only;
  * the Helm uninstall hook (a Role with delete on PlatformAgents) is absent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

DEMO_ANNOTATION = "kubeagents.x-k8s.io/controller-expert-demo"
DEMO_VOLUMES = frozenset({"controller-expert-demo-config", "controller-expert-demo-github"})
AGENT_CONTAINER = "platform-agent"
MANAGED_CONFIG_KEY = "managed-config.yaml"
READ_VERBS = frozenset({"get", "list", "watch"})
# Non-read grants on the agent ServiceAccount that upstream 0.16 makes and the
# demo keeps, with the reason each is not a workload or credential power.
ALLOWED_WRITE_RESOURCES = {
    "leases": "leader-election Lease objects in the agent namespace",
    "tokenreviews": "TokenReview create: the credential broker validates caller tokens; no object is stored",
}
SCHEDULE_KEYS = frozenset({"schedule", "cron"})
EMPTY_SCHEDULES = (None, "", [], {})


def load_docs(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]


def walk(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, child
            yield from walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")


def embedded_yaml(doc: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    if doc.get("kind") != "ConfigMap":
        return
    for key, text in (doc.get("data") or {}).items():
        if not isinstance(text, str) or not key.endswith((".yaml", ".yml", ".json")):
            continue
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        yield f"{doc['metadata']['name']}:{key}", parsed


def find_schedules(docs: list[dict[str, Any]]) -> list[str]:
    found = []
    for doc in docs:
        name = f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}"
        if doc.get("kind") == "CronJob":
            found.append(f"{name}: CronJob object")
        trees = [(name, doc)] + list(embedded_yaml(doc))
        for label, tree in trees:
            for key_path, value in walk(tree):
                leaf = key_path.rsplit(".", 1)[-1]
                if leaf in SCHEDULE_KEYS and value not in EMPTY_SCHEDULES:
                    found.append(f"{label}: {key_path}={value!r}")
    return found


def platform_agent(docs: list[dict[str, Any]]) -> dict[str, Any]:
    agents = [doc for doc in docs if doc.get("kind") == "PlatformAgent"]
    if len(agents) != 1:
        raise SystemExit(f"expected exactly one PlatformAgent, found {len(agents)}")
    return agents[0]


def agent_service_account(agent: dict[str, Any]) -> str:
    security = (agent.get("spec") or {}).get("security") or {}
    return security.get("serviceAccountName") or agent["metadata"]["name"]


def rbac_report(docs: list[dict[str, Any]], namespace: str, sa_name: str) -> tuple[list[str], list[str]]:
    roles = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs if doc.get("kind") in ("Role", "ClusterRole")}
    lines, problems = [], []
    for doc in docs:
        if doc.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        subjects = doc.get("subjects") or []
        if not any(s.get("kind") == "ServiceAccount" and s.get("name") == sa_name and s.get("namespace", namespace) == namespace for s in subjects):
            continue
        ref = doc["roleRef"]
        role = roles.get((ref["kind"], ref["name"]))
        if role is None:
            problems.append(f"{doc['kind']} {doc['metadata']['name']} references {ref['kind']} {ref['name']}, which is not in the render")
            continue
        for rule in role.get("rules") or []:
            verbs, resources = set(rule.get("verbs") or []), set(rule.get("resources") or [])
            lines.append(f"{ref['kind']} {ref['name']}: {sorted(verbs)} on {sorted(resources)}")
            if resources & {"secrets", "*"}:
                problems.append(f"{ref['name']} grants {sorted(verbs)} on secrets to {sa_name}")
            writes = verbs - READ_VERBS
            if writes and not resources <= set(ALLOWED_WRITE_RESOURCES):
                problems.append(f"{ref['name']} grants {sorted(writes)} on {sorted(resources)} to {sa_name}")
    if not lines:
        problems.append(f"no RBAC bound to {sa_name}; the operator runtime render is missing")
    return lines, problems


def check_manifests(path: Path) -> int:
    docs = load_docs(path)
    problems: list[str] = []
    agent = platform_agent(docs)
    namespace = agent["metadata"]["namespace"]
    sa_name = agent_service_account(agent)
    print(f"rendered objects: {len(docs)}")

    schedules = find_schedules(docs)
    print(f"cron schedules / CronJob objects: {len(schedules)}")
    problems += [f"schedule present: {item}" for item in schedules]

    lines, rbac_problems = rbac_report(docs, namespace, sa_name)
    print(f"RBAC bound to ServiceAccount {namespace}/{sa_name}:")
    for line in lines:
        print(f"  {line}")
    for resource, reason in ALLOWED_WRITE_RESOURCES.items():
        print(f"  allowed non-read: {resource} ({reason})")
    problems += rbac_problems

    if (agent["metadata"].get("annotations") or {}).get(DEMO_ANNOTATION) != "true":
        problems.append(f"PlatformAgent lacks {DEMO_ANNOTATION}: \"true\"")
    name = agent["metadata"]["name"]
    managed = next((doc for doc in docs if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == f"{name}-config"), None)
    managed_config = yaml.safe_load(((managed or {}).get("data") or {}).get(MANAGED_CONFIG_KEY) or "{}") or {}
    cron_mode = (managed_config.get("approvals") or {}).get("cron_mode")
    print(f"managed approvals.cron_mode: {cron_mode}")
    if cron_mode != "ask":
        problems.append(f"managed config cron_mode is {cron_mode!r}, not 'ask'")

    gateway = next((doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == f"{name}-gateway"), None)
    if gateway is None:
        problems.append("no gateway Deployment in the render")
    else:
        pod = gateway["spec"]["template"]["spec"]
        containers = (pod.get("initContainers") or []) + (pod.get("containers") or [])
        watcher = [env.get("value") for c in containers for env in c.get("env") or [] if env.get("name") == "EVENT_WATCHER_ENABLED"]
        print(f"EVENT_WATCHER_ENABLED: {watcher}")
        if not watcher or any(value != "false" for value in watcher):
            problems.append(f"event watcher not disabled: {watcher}")
        volumes = {v["name"] for v in pod.get("volumes") or []}
        if not DEMO_VOLUMES <= volumes:
            problems.append(f"gateway pod lacks demo volumes {sorted(DEMO_VOLUMES - volumes)}")
        for container in containers:
            for mount in container.get("volumeMounts") or []:
                if mount["name"] in DEMO_VOLUMES:
                    if container["name"] != AGENT_CONTAINER:
                        problems.append(f"demo volume {mount['name']} mounted into {container['name']}")
                    if not mount.get("readOnly"):
                        problems.append(f"demo volume {mount['name']} is writable in {container['name']}")
        image = next((c.get("image") for c in pod.get("containers") or [] if c["name"] == AGENT_CONTAINER), None)
        print(f"platform-agent image: {image}")

    hooks = [doc["metadata"]["name"] for doc in docs if "helm.sh/hook" in ((doc.get("metadata") or {}).get("annotations") or {})]
    if hooks:
        problems.append(f"Helm hooks present (the uninstall hook must be disabled): {hooks}")

    for problem in problems:
        print(f"FAIL {problem}")
    print("RESULT: PASS" if not problems else f"RESULT: FAIL ({len(problems)})")
    return 0 if not problems else 1


def extract_cr(rendered: Path, out: Path) -> int:
    agent = platform_agent(load_docs(rendered))
    out.write_text(yaml.safe_dump(agent, sort_keys=False))
    return 0


def check_profile(base: Path, overlay: Path, out: Path | None) -> int:
    """Rebuild the image's demo profile config from upstream sources and check it."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    sys.path.insert(0, str(here.parents[1] / "deploy" / "docker"))
    import apply_demo_profile  # noqa: E402
    import merge_configs  # noqa: E402

    merged = merge_configs.merge(yaml.safe_load(base.read_text()) or {}, yaml.safe_load(overlay.read_text()) or {})
    config = apply_demo_profile.demo_profile_config(merged)
    apply_demo_profile.check_demo_profile_config(config)
    print("demo profile mcp_servers:", sorted(config["mcp_servers"]))
    print("demo profile platform_toolsets:", json.dumps(config["platform_toolsets"], sort_keys=True))
    print("demo profile top-level toolsets:", config["toolsets"])
    print("demo profile disabled_toolsets:", ", ".join(config["agent"]["disabled_toolsets"]))
    if out:
        out.write_text(yaml.safe_dump(config, sort_keys=False))
    print("RESULT: PASS")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("extract-cr")
    p.add_argument("rendered", type=Path)
    p.add_argument("out", type=Path)
    p = sub.add_parser("manifests")
    p.add_argument("rendered", type=Path)
    p = sub.add_parser("profile")
    p.add_argument("base", type=Path)
    p.add_argument("overlay", type=Path)
    p.add_argument("out", type=Path, nargs="?")
    args = parser.parse_args(argv)
    if args.command == "extract-cr":
        return extract_cr(args.rendered, args.out)
    if args.command == "manifests":
        return check_manifests(args.rendered)
    return check_profile(args.base, args.overlay, args.out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
