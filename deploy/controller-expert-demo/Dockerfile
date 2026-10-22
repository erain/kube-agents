# Request-driven GKE controller-expert demo image.
#
# A thin overlay on the released upstream Platform Agent image, pinned by digest,
# so every byte below the overlay is exactly kube-agents 0.16.0. The overlay:
#
#   1. empties both cron rosters (Planning Agent and Platform Agent), so a fresh
#      volume carries no schedule for any ticker to advance;
#   2. replaces the Platform Agent profile's MCP servers and toolsets with one
#      demo MCP server exposing one tool (apply_demo_profile.py);
#   3. adds the deterministic controller and the promoted routing config under
#      /opt/controller-expert-demo, root-owned and read-only;
#   4. adds pinned, checksum-verified kubeconform and kube-linter binaries.
#
# Build from the repository root:
#   docker build -f deploy/controller-expert-demo/Dockerfile .
# deploy/controller-expert-demo/cloudbuild.yaml is the recorded build.

ARG BASE_IMAGE=ghcr.io/gke-labs/kube-agents/platform-agent:0.16.0@sha256:1194c591400cfcb813546024a5abc579c6b65be165d57d7f069a736885650eeb
ARG TOOLS_IMAGE=docker.io/library/alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

FROM ${TOOLS_IMAGE} AS validators
ARG KUBECONFORM_VERSION=v0.8.0
ARG KUBECONFORM_SHA256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883
ARG KUBE_LINTER_VERSION=v0.8.3
ARG KUBE_LINTER_SHA256=618d299a3e2839c8ca9d86fce0db617be0fba41f0fecbbbfb7fbf1c04299fae1
RUN set -eu; \
    mkdir -p /out; \
    wget -q -O /tmp/kubeconform.tar.gz "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz"; \
    echo "${KUBECONFORM_SHA256}  /tmp/kubeconform.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/kubeconform.tar.gz -C /out kubeconform; \
    wget -q -O /out/kube-linter "https://github.com/stackrox/kube-linter/releases/download/${KUBE_LINTER_VERSION}/kube-linter-linux"; \
    echo "${KUBE_LINTER_SHA256}  /out/kube-linter" | sha256sum -c -; \
    chmod 0755 /out/kubeconform /out/kube-linter

FROM ${BASE_IMAGE} AS platform-expert-demo
USER root
COPY --from=validators /out/kubeconform /out/kube-linter /usr/local/bin/
COPY deploy/controller-expert-demo/controller_expert_demo.py \
     deploy/controller-expert-demo/server.py \
     deploy/controller-expert-demo/routing.json \
     deploy/controller-expert-demo/apply_demo_profile.py \
     /opt/controller-expert-demo/
COPY deploy/controller-expert-demo/profile/ /opt/controller-expert-demo/profile/
# The overlay, then three read-backs that fail the build rather than ship a
# demo image that still carries autonomy:
#   - the image's own Hermes resolves the demo profile's toolsets to the one MCP
#     server, for both a kanban worker (`cli`) and the API server;
#   - neither roster holds a job;
#   - the validators run.
RUN set -eu; \
    /opt/hermes/.venv/bin/python3 /opt/controller-expert-demo/apply_demo_profile.py \
        --profile-dir /opt/controller-expert-demo/profile; \
    cd /opt/hermes && HERMES_HOME=/opt/platform-template /opt/hermes/.venv/bin/python3 -c "\
from hermes_cli.config import load_config; \
from hermes_cli.tools_config import _get_platform_tools; \
cfg = load_config(); \
resolved = {key: sorted(_get_platform_tools(cfg, key)) for key in ('cli', 'api_server')}; \
print('demo profile toolsets:', resolved); \
allowed = {'mcp-controller_expert_demo', 'controller_expert_demo'}; \
assert all(resolved[key] and set(resolved[key]) <= allowed for key in resolved), resolved"; \
    for roster in /opt/defaults/cron/jobs.json /opt/platform-template/cron/jobs.json; do \
        /opt/hermes/.venv/bin/python3 -c "import json, sys; d = json.load(open(sys.argv[1])); assert d == {'jobs': []}, (sys.argv[1], d)" "${roster}"; \
    done; \
    kubeconform -v; \
    kube-linter version; \
    chown -R root:root /opt/controller-expert-demo /opt/platform-template; \
    chmod -R a+rX,go-w /opt/controller-expert-demo /opt/platform-template; \
    chown -R hermes:hermes /opt/defaults/cron
USER 10000:10000
