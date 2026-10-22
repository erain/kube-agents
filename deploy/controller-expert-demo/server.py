#!/opt/hermes/.venv/bin/python3
"""MCP server for the controller-expert demo: exactly one tool.

This is the only MCP server the demo Platform Agent profile loads. It exposes a
single tool that runs one frozen scenario through the deterministic controller
in controller_expert_demo.py. The tool takes a scenario id and nothing else, so
the orchestrating model can choose which frozen scenario to run but cannot
supply a prompt, a model, a file, a path, a value, or a command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server import MCPServer

# Same directory as this script; Python puts it on sys.path[0] when it runs.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import controller_expert_demo as demo  # noqa: E402

SERVER_NAME = "GKE Controller Expert Demo"

mcp = MCPServer(SERVER_NAME)


@mcp.tool()
def run_controller_expert_demo(scenario_id: str) -> str:
    """Run one frozen GKE controller-expert scenario through the routed expert and GitOps.

    Call this exactly once per scenario the user names. The deterministic
    controller routes the scenario's workflow to the promoted expert (the
    fine-tuned specialist for troubleshooting and optimization, the stock model
    for generation), makes one live expert call, validates the structured action
    (allowlisted paths, minimal semantic diff, strict kubeconform, kube-linter),
    and opens a draft pull request on the GitOps repository. It never mutates a
    cluster. A scenario that was already attempted is refused, and a failed
    scenario must be reported as failed, not retried.

    Args:
        scenario_id: The frozen scenario identifier the user asked for.
    """
    try:
        result = demo.run(scenario_id)
    except demo.DemoRefused as exc:
        demo.log(f"refused scenario={scenario_id} reason={exc}")
        return json.dumps({"status": "REFUSED", "error": str(exc), "direct_cluster_mutation": False}, sort_keys=True)
    except Exception as exc:  # before the budget reservation: infrastructure, not a model outcome
        demo.log(f"error scenario={scenario_id} reason={type(exc).__name__}: {exc}")
        return json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}", "direct_cluster_mutation": False}, sort_keys=True)
    return json.dumps(demo.summary(result), sort_keys=True)


if __name__ == "__main__":
    mcp.run()
