"""Read the JSON values needed by the PowerShell preflight script."""

import json
import sys


def load_json(filename: str):
    with open(filename, "r", encoding="utf-8") as handle:
        return json.load(handle)


if len(sys.argv) != 3:
    print("Usage: inspect_preflight_json.py CONFIG_JSON MCP_JSON", file=sys.stderr)
    raise SystemExit(2)

config = load_json(sys.argv[1])
servers = load_json(sys.argv[2])
source = config.get("source") or {}
record = source.get("record") or {}
interact = config.get("interact") or {}
print(f"config_record_enabled={str(bool(record.get('enabled'))).lower()}")
print(f"config_play_sound={str(bool(interact.get('playSound'))).lower()}")
print(f"config_automatic_player_status={str(bool(source.get('automatic_player_status'))).lower()}")
print(f"mcp_autostart_count={sum(1 for server in servers if server.get('autostart'))}")
