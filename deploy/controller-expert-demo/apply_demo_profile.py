#!/usr/bin/env python3
"""Turn an upstream Platform Agent image into the request-driven expert demo image.

Run once at image build time, as root, against the image's own templates:

  * both cron rosters (the Planning Agent's /opt/defaults/cron/jobs.json and the
    Platform Agent's /opt/platform-template/cron/jobs.json) become empty, so no
    schedule exists for any ticker to advance on a fresh volume;
  * the Platform Agent profile config keeps only the demo MCP server: no
    terminal, file, web, skills, delegation, cronjob or kanban-orchestrator
    toolsets, no GKE or Developer Knowledge MCP;
  * the Platform Agent persona is replaced by a short demo persona and roster
    description so the Planning Agent routes only demo requests there.

Every rewrite is read back and asserted; a mismatch fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

DEMO_SERVER = "controller_expert_demo"
DEMO_TOOLSET = f"mcp-{DEMO_SERVER}"
DEMO_SERVER_COMMAND = "/opt/hermes/.venv/bin/python3"
DEMO_SERVER_SCRIPT = "/opt/controller-expert-demo/server.py"
DEMO_CONFIG_DIR = "/etc/controller-expert-demo"
# One scenario per card: the call, a summary, the kanban completion. Headroom
# for a refusal message, never enough for an open-ended investigation.
DEMO_MAX_TURNS = 12
# Long enough for kubeconform's schema download and one 300 s expert call.
DEMO_TOOL_TIMEOUT_SECONDS = 900
DEMO_CONNECT_TIMEOUT_SECONDS = 60
# The platform keys a worker or the API server resolves toolsets from.
PLATFORM_KEYS = ("cli", "api_server")
# Hermes toolsets the demo profile must never offer. The Chat Agent's own
# denylist (agents/chat/config.yaml) plus the kanban orchestrator surface; a
# kanban worker still receives its task-scoped lifecycle tools from
# HERMES_KANBAN_TASK, which is how it completes the card.
DENIED_TOOLSETS = (
    "browser", "code_execution", "computer_use", "cronjob", "delegation", "discord",
    "discord_admin", "file", "homeassistant", "image_gen", "project", "search",
    "session_search", "skills", "spotify", "terminal", "todo", "tts", "video",
    "video_gen", "vision", "web", "x_search",
)
KEPT_PLUGINS = ("hermes_otel", "tool_call_audit")
EMPTY_ROSTER = {"jobs": []}
CHAT_ROSTER = "/opt/defaults/cron/jobs.json"
PLATFORM_TEMPLATE = "/opt/platform-template"


def demo_profile_config(upstream: dict) -> dict:
    """The upstream platform profile config with only the demo surface left."""
    config = dict(upstream)
    config["mcp_servers"] = {
        DEMO_SERVER: {
            "command": DEMO_SERVER_COMMAND,
            "args": [DEMO_SERVER_SCRIPT],
            "connect_timeout": DEMO_CONNECT_TIMEOUT_SECONDS,
            "timeout": DEMO_TOOL_TIMEOUT_SECONDS,
            "env": {
                "HERMES_HOME": "${HERMES_HOME}",
                "CONTROLLER_EXPERT_DEMO_CONFIG_DIR": DEMO_CONFIG_DIR,
            },
        },
    }
    config["platform_toolsets"] = {key: [DEMO_TOOLSET] for key in PLATFORM_KEYS}
    # No kanban orchestrator surface: the demo specialist cannot file cards.
    config["toolsets"] = []
    agent = dict(config.get("agent") or {})
    agent["max_turns"] = DEMO_MAX_TURNS
    agent["disabled_toolsets"] = sorted(set(agent.get("disabled_toolsets") or []) | set(DENIED_TOOLSETS))
    config["agent"] = agent
    config["memory"] = {"memory_enabled": False, "provider": "", "read_only": True, "user_profile_enabled": False}
    plugins = dict(config.get("plugins") or {})
    plugins["enabled"] = [name for name in (plugins.get("enabled") or []) if name in KEPT_PLUGINS]
    config["plugins"] = plugins
    return config


def check_demo_profile_config(config: dict) -> None:
    assert set(config["mcp_servers"]) == {DEMO_SERVER}, config["mcp_servers"]
    for key in PLATFORM_KEYS:
        assert config["platform_toolsets"][key] == [DEMO_TOOLSET], config["platform_toolsets"]
    assert set(config["platform_toolsets"]) == set(PLATFORM_KEYS), config["platform_toolsets"]
    assert not config["toolsets"], config["toolsets"]
    assert set(DENIED_TOOLSETS) <= set(config["agent"]["disabled_toolsets"])
    assert config["agent"]["max_turns"] == DEMO_MAX_TURNS
    assert config["memory"]["memory_enabled"] is False


def write_roster(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EMPTY_ROSTER) + "\n")
    assert json.loads(path.read_text()) == EMPTY_ROSTER, path


def apply(root: Path, profile_dir: Path) -> None:
    chat_roster = root / CHAT_ROSTER.lstrip("/")
    template = root / PLATFORM_TEMPLATE.lstrip("/")
    write_roster(chat_roster)
    write_roster(template / "cron" / "jobs.json")
    config_path = template / "config.yaml"
    config = demo_profile_config(yaml.safe_load(config_path.read_text()) or {})
    check_demo_profile_config(config)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    check_demo_profile_config(yaml.safe_load(config_path.read_text()))
    for name in ("SOUL.md", "AGENTS.md", "CAPABILITIES.md"):
        (template / name).write_text((profile_dir / name).read_text())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--profile-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    apply(args.root, args.profile_dir)
    print("controller-expert demo profile applied: empty cron rosters, one MCP tool")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
