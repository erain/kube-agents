#!/usr/bin/env bash
# Render the controller-expert demo exactly as it would deploy, then check it.
#
#   1. helm template the upstream chart with the demo values (plus any extra
#      helm arguments given on the command line);
#   2. reconcile the chart-rendered PlatformAgent with the real operator against
#      envtest and capture every object the operator writes (its RBAC, managed
#      config, Deployments, NetworkPolicies, ...);
#   3. concatenate both into one rendered.yaml and run check_rendered.py on it;
#   4. rebuild the demo profile config from upstream sources and check it.
#
# Usage: deploy/controller-expert-demo/render.sh OUT_DIR [helm args...]
# No cluster, no network beyond the Go module and envtest caches, no model call.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly RELEASE_NAME="kube-agents"
readonly NAMESPACE="kubeagents-system"
readonly PYTHON="${PYTHON:-python3}"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 OUT_DIR [helm args...]" >&2
  exit 2
fi
readonly OUT_DIR="$1"
shift
mkdir -p "${OUT_DIR}"

helm template "${RELEASE_NAME}" "${REPO_ROOT}/charts/kube-agents" -n "${NAMESPACE}" \
  -f "${SCRIPT_DIR}/values.yaml" "$@" > "${OUT_DIR}/chart.yaml"
"${PYTHON}" "${SCRIPT_DIR}/check_rendered.py" extract-cr "${OUT_DIR}/chart.yaml" "${OUT_DIR}/platformagent.yaml"

assets="$(make -s -C "${REPO_ROOT}/k8s-operator" envtest-path | tail -n 1)"
[[ -d "${assets}" ]] || { echo "envtest assets not found: ${assets}" >&2; exit 1; }
(
  cd "${REPO_ROOT}/k8s-operator"
  KUBEBUILDER_ASSETS="${assets}" \
  CONTROLLER_EXPERT_DEMO_CR="$(cd "${OUT_DIR}" && pwd)/platformagent.yaml" \
  CONTROLLER_EXPERT_DEMO_RENDER_OUT="$(cd "${OUT_DIR}" && pwd)/operator-runtime.yaml" \
    go test ./internal/controller/ -run '^TestControllerExpertDemoRuntimeRenderEnvtest$' -count=1 -v
)

{
  echo "# controller-expert demo: chart render (helm template, demo values)"
  cat "${OUT_DIR}/chart.yaml"
  echo "---"
  cat "${OUT_DIR}/operator-runtime.yaml"
} > "${OUT_DIR}/rendered.yaml"

"${PYTHON}" "${SCRIPT_DIR}/check_rendered.py" manifests "${OUT_DIR}/rendered.yaml"
"${PYTHON}" "${SCRIPT_DIR}/check_rendered.py" profile \
  "${REPO_ROOT}/deploy/shared/defaults/config.yaml" \
  "${REPO_ROOT}/agents/platform/config.yaml" \
  "${OUT_DIR}/demo-profile-config.yaml"
