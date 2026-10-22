# Controller-expert demo (fork-only)

A request-driven demo of a routed private GKE expert inside kube-agents 0.16.0.
Not an upstream feature: it lives on the `erain/kube-agents` branch
`feat/expert-demo-0.16` and is off unless a PlatformAgent carries
`kubeagents.x-k8s.io/controller-expert-demo: "true"`.

```text
chat / Kube Agents Console / API
  -> Planning Agent (upstream, unchanged)
  -> kanban card -> demo Platform Agent profile (one MCP tool)
  -> run_controller_expert_demo(scenario_id)
       deterministic controller: route by workflow (routing.json)
       -> one call to the private expert (OpenAI-compatible, no retry)
       -> strict action contract, allowlisted paths, minimal semantic diff
       -> strict kubeconform + kube-linter
       -> GitHub REST: branch, commit, draft PR
```

## What makes it request-driven

| Layer | Where | Effect |
|---|---|---|
| Image | `Dockerfile`, `apply_demo_profile.py` | Both cron rosters empty; demo profile toolsets resolve (by the image's own Hermes, at build) to the one MCP server; no terminal/file/web/skills/cronjob/delegation/kanban-orchestrator toolsets |
| Operator | `k8s-operator/internal/controller/controller_expert_demo.go` | Managed `approvals.cron_mode: ask`; k8s-event-watcher never started; demo ConfigMap + GitHub Secret mounted read-only into `platform-agent` only |
| Chart values | `values.yaml` | Demo annotation, demo images, uninstall hook off |
| Tool | `controller_expert_demo.py` | Refuses if any cron job exists on the volume; one expert call per scenario, never retried; global expert-call budget in a PVC ledger, logged to `logs/controller-expert-demo.log` (Cloud Logging via fluent-bit) |
| RBAC | upstream operator, unchanged | Agent ServiceAccount: reads only, no Secret verb (leases and TokenReview create are the only non-read grants) |

## Inputs (created by the deployer, never in the image)

- ConfigMap `controller-expert-demo` in the agent namespace: `demo.json` (expert URL,
  GitHub repository, base branch, branch prefix, budget, kube-linter gate, scenario
  pack SHA-256) and `scenarios.jsonl` (frozen scenarios; any `expected`/`assertions`
  key is refused so the gate oracle stays out of the cluster).
- Secret `controller-expert-demo-github`, key `token`: a credential that can push a
  branch and open a draft PR on the GitOps repository only.

## Build, render, test

```bash
# tests (no network, no model)
python3 -m unittest discover -s deploy/controller-expert-demo -p 'test_*.py'
KUBEBUILDER_ASSETS="$(make -s -C k8s-operator envtest-path | tail -n1)" \
  go -C k8s-operator test ./internal/controller/ -run ControllerExpertDemo
# render chart + operator runtime objects and check the invariants
deploy/controller-expert-demo/render.sh /tmp/expert-demo-render
# images, from exactly one commit
stage="$(mktemp -d)"; git archive HEAD deploy/controller-expert-demo k8s-operator | tar -x -C "$stage"
gcloud builds submit "$stage" --config deploy/controller-expert-demo/cloudbuild.yaml \
  --substitutions=_TAG=expert-demo-0.16.0-$(git rev-parse --short=7 HEAD)
```
