"""Tiny stub MCP server speaking newline-delimited JSON-RPC 2.0 over stdio.

Used by the ONE real-subprocess integration test in tests/test_mcp.py. Supports
the handshake (initialize + notifications/initialized), tools/list (one tool
named 'echo'), and tools/call (echoes its arguments back). Anything else gets a
JSON-RPC method-not-found error. Exits when stdin closes.
"""
from __future__ import annotations

import json
import sys


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications (no id) -> no response.
        if msg_id is None:
            continue

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub-mcp", "version": "0.0.1"},
                },
            })
        elif method == "tools/list":
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo the provided arguments back.",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            })
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                _send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(arguments)}],
                        "isError": False,
                    },
                })
            else:
                _send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                })
        else:
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })


if __name__ == "__main__":
    main()
