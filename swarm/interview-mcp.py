#!/usr/bin/env python3
"""Tiny MCP server providing an end_interview tool.

When Claude calls end_interview, this server writes a sentinel file that
swarm.py polls for. swarm.py then sends SIGINT to Claude Code, ending the
interactive session automatically.

Implements the MCP stdio transport (JSON-RPC 2.0, newline-delimited) using
only the Python standard library.
"""

import json
import os
import sys

SERVER_NAME = "interview"
PROTOCOL_VERSION = "2024-11-05"


def handle_initialize(req_id):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
        },
    }


def handle_tools_list(req_id):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "tools": [
                {
                    "name": "end_interview",
                    "description": (
                        "End the interview session. Call this after "
                        "PROJECT.md has been written."
                    ),
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        },
    }


def handle_tools_call(req_id, params):
    name = params.get("name", "")
    if name != "end_interview":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown tool: {name}"},
        }

    sentinel = os.environ.get("INTERVIEW_SENTINEL", "")
    if sentinel:
        with open(sentinel, "w") as f:
            f.write("done")

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [
                {"type": "text", "text": "Interview complete. Session ending."}
            ]
        },
    }


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        req_id = msg.get("id")

        # Notifications (no id) — just acknowledge silently
        if req_id is None:
            continue

        if method == "initialize":
            send(handle_initialize(req_id))
        elif method == "tools/list":
            send(handle_tools_list(req_id))
        elif method == "tools/call":
            send(handle_tools_call(req_id, msg.get("params", {})))
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        else:
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


if __name__ == "__main__":
    main()
