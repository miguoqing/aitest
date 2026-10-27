#!/usr/bin/env python3
"""最小 MCP stdio server，仅供 governance 的握手测试使用。"""

import json
import sys

TOOLS = [
    {
        "name": "ping",
        "description": "returns pong",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            continue  # notification，不回
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-server", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
